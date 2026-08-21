# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - SCHEDULE & METRICS ENGINE SERVICE
# ==============================================================================
# Description: Generates date-specific daily lecture schedules, resolves teacher leaves,
#              handles holidays and proxy duties, and calculates strict attendance metrics.
# ==============================================================================

from datetime import date, datetime
from extensions import db, get_current_date
from models import Timetable, Holiday, TeacherLeave, DailySchedule, AttendanceSession, AttendanceRecord, Student

# ==============================================================================
# 1. DAILY SCHEDULE GENERATION & STATUS RESOLUTION
# ==============================================================================
def generate_daily_schedule(target_date=None):
    """
    Expands recurring timetable entries into daily_schedule rows for target_date.
    Resolves status to HOLIDAY, TEACHER_ON_LEAVE, SUBSTITUTE_ASSIGNED, CANCELLED, or SCHEDULED.
    
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
    global_holiday = any(h.scope == 'ALL' or str(h.scope).upper() == 'ALL' for h in holidays)
    class_holiday_ids = set()
    for h in holidays:
        if h.scope and h.scope != 'ALL':
            try:
                class_holiday_ids.add(int(h.scope))
            except ValueError:
                pass

    # Pre-fetch approved teacher leaves for target_date
    approved_leaves = TeacherLeave.query.filter(
        TeacherLeave.status == 'APPROVED',
        TeacherLeave.date_from <= target_date,
        TeacherLeave.date_to >= target_date
    ).all()
    leave_by_teacher = {l.teacher_id: l for l in approved_leaves}

    resolved_rows = []
    for tt in timetables:
        # Check if already generated
        existing = DailySchedule.query.filter_by(date=target_date, timetable_id=tt.id).first()
        if not existing:
            existing = DailySchedule(date=target_date, timetable_id=tt.id)

        # Resolve status if not manually overridden by admin/proxy
        if existing.is_cancelled:
            existing.resolved_status = 'CANCELLED'
        elif existing.is_proxy and existing.substitute_teacher_id:
            existing.resolved_status = 'SUBSTITUTE_ASSIGNED'
        elif global_holiday or (tt.class_id in class_holiday_ids):
            existing.resolved_status = 'HOLIDAY'
        elif tt.teacher_id in leave_by_teacher:
            leave = leave_by_teacher[tt.teacher_id]
            if leave.substitute_teacher_id:
                existing.substitute_teacher_id = leave.substitute_teacher_id
                existing.resolved_status = 'SUBSTITUTE_ASSIGNED'
            else:
                existing.resolved_status = 'TEACHER_ON_LEAVE'
        else:
            existing.resolved_status = 'SCHEDULED'

        db.session.add(existing)
        resolved_rows.append(existing)

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
