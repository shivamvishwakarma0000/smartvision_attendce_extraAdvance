# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - SCHEDULE & METRICS ENGINE SERVICE
# ==============================================================================
# Description: Generates date-specific daily lecture schedules, resolves teacher leaves,
#              handles holidays and proxy duties, and calculates strict attendance metrics.
# ==============================================================================

from datetime import date, datetime, timedelta, time
from extensions import db, get_current_date
from models import Timetable, Holiday, TeacherLeave, DailySchedule, AttendanceSession, AttendanceRecord, Student, Teacher, TeacherDailyAttendance

def convert_to_24h(time_str):
    """
    Parses any time format (e.g. '03:05', '3:05', '03:05 PM', '3:05 PM', '15:05')
    and returns a standardized 24-hour string 'HH:MM'.
    Defaulting heuristic: If no AM/PM is specified and hour is between 1 and 6,
    it is treated as PM (post-lunch academic session e.g. 03:05 is 15:05 PM).
    """
    if not time_str:
        return "00:00"
    t_clean = time_str.strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%I:%M"):
        try:
            dt = datetime.strptime(t_clean, fmt)
            if fmt in ("%I:%M", "%H:%M") and ('AM' not in t_clean.upper() and 'PM' not in t_clean.upper()):
                if 1 <= dt.hour <= 6:
                    dt = dt.replace(hour=dt.hour + 12)
            return dt.strftime('%H:%M')
        except ValueError:
            pass
    return "00:00"

# ==============================================================================
# 1. DAILY SCHEDULE GENERATION & STATUS RESOLUTION
# ==============================================================================
def generate_daily_schedule(target_date=None):
    """
    Expands recurring timetable entries into daily_schedule rows for target_date.
    Resolves status to HOLIDAY, TEACHER_ON_LEAVE, SUBSTITUTE_ASSIGNED, CANCELLED, or SCHEDULED.
    Enforces automatic cancellation at scheduled class start time if absent faculty has no proxy.
    
    Parameters:
        target_date (date or str, optional): The target date to generate schedules for.
        
    Returns:
        list: Resolved DailySchedule database model instances.
    """
    if target_date is None:
        target_date = get_current_date()
    elif isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()

    day_name = target_date.strftime('%A') # e.g. "Monday"

    # Query active timetable entries for this day of week
    query = Timetable.query.filter(
        Timetable.day_of_week == day_name,
        (Timetable.effective_from == None) | (Timetable.effective_from <= target_date),
        (Timetable.effective_to == None) | (Timetable.effective_to >= target_date)
    )
    timetables = query.all()

    # Pre-fetch global holidays and class holidays for target_date
    holidays = Holiday.query.filter_by(date=target_date).all()
    global_holiday = any(h.scope in ('ALL', 'CLASSES_ONLY', 'STUDENTS_ONLY') or str(h.scope).upper() in ('ALL', 'CLASSES_ONLY', 'STUDENTS_ONLY') for h in holidays)
    class_holiday_ids = set()
    for h in holidays:
        if h.scope and h.scope not in ('ALL', 'CLASSES_ONLY', 'STUDENTS_ONLY'):
            try:
                class_holiday_ids.add(int(h.scope))
            except ValueError:
                pass

    # Pre-fetch approved teacher leaves for target_date
    approved_leaves = TeacherLeave.query.filter(
        TeacherLeave.status.in_(['APPROVED', 'Approved']),
        TeacherLeave.date_from <= target_date,
        TeacherLeave.date_to >= target_date
    ).all()
    leave_by_teacher = {l.teacher_id: l for l in approved_leaves}

    # Pre-fetch faculty attendance records to determine absent teachers for target_date
    absent_teacher_ids = set(leave_by_teacher.keys())
    today = get_current_date()
    now_dt = datetime.now()
    current_time_24h = convert_to_24h(now_dt.strftime('%H:%M'))

    today_recs = TeacherDailyAttendance.query.filter_by(attendance_date=target_date).all()
    for rec in today_recs:
        if rec.status in ('Absent', 'Approved Leave'):
            absent_teacher_ids.add(rec.teacher_id)

    # If target_date is today and past morning cutoff, faculty without check-in are treated as absent
    try:
        from teacher_attendance.routes import get_or_create_settings, parse_time_str
        settings = get_or_create_settings()
        morn_deadline = parse_time_str(settings.morning_deadline) or time(9, 0)
        grace_mins = settings.grace_period_mins if settings.grace_period_mins is not None else 30
        cutoff_dt = datetime.combine(target_date, morn_deadline) + timedelta(minutes=grace_mins)
        if target_date < today or (target_date == today and now_dt > cutoff_dt):
            checked_teacher_ids = {r.teacher_id for r in today_recs}
            all_teachers = Teacher.query.all()
            for t in all_teachers:
                if t.id not in checked_teacher_ids:
                    absent_teacher_ids.add(t.id)
    except Exception:
        pass

    resolved_rows = []
    daily_schedules_by_tt = {}

    for tt in timetables:
        # Check if already generated
        existing = DailySchedule.query.filter_by(date=target_date, timetable_id=tt.id).first()
        if not existing:
            existing = DailySchedule(date=target_date, timetable_id=tt.id)

        slot_start_24h = convert_to_24h(tt.start_time)
        is_teacher_absent = (tt.teacher_id in absent_teacher_ids) if tt.teacher_id else False

        # Resolve status if not manually overridden by admin/proxy
        if existing.is_cancelled:
            existing.resolved_status = 'CANCELLED'
        elif existing.is_proxy and existing.substitute_teacher_id:
            existing.resolved_status = 'SUBSTITUTE_ASSIGNED'
        elif global_holiday or (tt.class_id in class_holiday_ids):
            existing.resolved_status = 'HOLIDAY'
        elif is_teacher_absent:
            # Rule 3 & 4: If scheduled start time arrived and no proxy assigned, auto-cancel
            if target_date == today and current_time_24h >= slot_start_24h:
                existing.is_cancelled = True
                existing.resolved_status = 'CANCELLED'
                existing.cancellation_reason = f"Auto-cancelled: Absent faculty with no proxy assigned by start time ({tt.start_time})"
            elif tt.teacher_id in leave_by_teacher and leave_by_teacher[tt.teacher_id].substitute_teacher_id:
                existing.substitute_teacher_id = leave_by_teacher[tt.teacher_id].substitute_teacher_id
                existing.resolved_status = 'SUBSTITUTE_ASSIGNED'
            else:
                existing.resolved_status = 'TEACHER_ON_LEAVE'
        else:
            existing.resolved_status = 'SCHEDULED'

        db.session.add(existing)
        resolved_rows.append(existing)
        daily_schedules_by_tt[tt.id] = existing

    # Synchronize linked Lab slots (Period P and P+1)
    for tt in timetables:
        if tt.linked_slot_id and tt.linked_slot_id in daily_schedules_by_tt:
            ds_this = daily_schedules_by_tt[tt.id]
            ds_linked = daily_schedules_by_tt[tt.linked_slot_id]
            # If one is cancelled, sync cancellation to the other
            if ds_this.is_cancelled and not ds_linked.is_cancelled:
                ds_linked.is_cancelled = True
                ds_linked.resolved_status = 'CANCELLED'
                ds_linked.cancellation_reason = ds_this.cancellation_reason
            elif ds_linked.is_cancelled and not ds_this.is_cancelled:
                ds_this.is_cancelled = True
                ds_this.resolved_status = 'CANCELLED'
                ds_this.cancellation_reason = ds_linked.cancellation_reason
            # If one is proxy assigned, sync proxy to the other
            elif ds_this.is_proxy and ds_this.substitute_teacher_id and not ds_linked.is_proxy:
                ds_linked.is_proxy = True
                ds_linked.substitute_teacher_id = ds_this.substitute_teacher_id
                ds_linked.resolved_status = 'SUBSTITUTE_ASSIGNED'
            elif ds_linked.is_proxy and ds_linked.substitute_teacher_id and not ds_this.is_proxy:
                ds_this.is_proxy = True
                ds_this.substitute_teacher_id = ds_linked.substitute_teacher_id
                ds_this.resolved_status = 'SUBSTITUTE_ASSIGNED'

    db.session.commit()
    return resolved_rows

# ==============================================================================
# 2. STRICT STUDENT ATTENDANCE PERCENTAGE CALCULATION
# ==============================================================================
def calculate_student_attendance(student_id, subject_id=None, class_id=None):
    """
    NON-NEGOTIABLE CORE ATTENDANCE FORMULA:
    Attendance % = COUNT(attendance_records WHERE status = PRESENT)
                 / COUNT(attendance_sessions WHERE status = COMPLETED)
                 * 100

    A slot that was never opened, was cancelled, fell on a holiday, or is a non-teaching
    period (lunch/break) MUST NEVER enter the denominator.
    
    Parameters:
        student_id (int): Primary key of the student
        subject_id (int, optional): Filter by specific subject
        class_id (int, optional): Filter by class section
        
    Returns:
        dict: Attended sessions, completed sessions, percentage, and missed count.
    """
    student = Student.query.get(student_id)
    if not student:
        return {'attended': 0, 'completed_sessions': 0, 'percentage': 0.0, 'missed': 0}

    target_class_id = class_id or student.class_id

    # Query completed attendance sessions for student's class
    sessions_query = db.session.query(AttendanceSession).filter(
        AttendanceSession.class_id == target_class_id,
        AttendanceSession.status == 'COMPLETED'
    )

    if subject_id:
        sessions_query = sessions_query.filter(AttendanceSession.subject_id == subject_id)

    completed_sessions_list = sessions_query.all()
    completed_session_ids = [s.id for s in completed_sessions_list]
    completed_count = len(completed_session_ids)

    if completed_count == 0:
        return {'attended': 0, 'completed_sessions': 0, 'percentage': 0.0, 'missed': 0}

    # Query present records for this student in those completed sessions
    present_count = AttendanceRecord.query.filter(
        AttendanceRecord.session_id.in_(completed_session_ids),
        AttendanceRecord.student_id == student_id,
        AttendanceRecord.status == 'PRESENT'
    ).count()

    percentage = round((present_count / completed_count * 100), 2)
    missed = max(0, completed_count - present_count)

    return {
        'attended': present_count,
        'completed_sessions': completed_count,
        'percentage': percentage,
        'missed': missed
    }
