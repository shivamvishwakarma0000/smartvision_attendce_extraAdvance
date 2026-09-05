# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - MAIN & ADMIN ROUTING MODULE
# ==============================================================================
# Description: Administrative control center encompassing overall dashboard pulses,
#              emergency proxy & reallocation desk, institutional approvals,
#              attendance audits, timetable configuration, and AI face recognition.
# ==============================================================================

import os
import io
import base64
try:
    import face_recognition
except ImportError:
    face_recognition = None
import numpy as np
from datetime import date, datetime, time, timedelta
from flask import render_template, redirect, url_for, flash, request, send_from_directory, send_file, jsonify, Blueprint, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from collections import defaultdict
from functools import wraps

from extensions import db, get_current_date, get_current_time_str, get_current_datetime_str
from models import (
    User, Class, Teacher, Subject, Student, Attendance, ClassAnnouncement, StudentEditRequest, IssuedTeacherID,
    TeacherAssignment, Timetable, Holiday, TeacherLeave, DailySchedule,
    AttendanceSession, AttendanceRecord, CorrectionRequest, AttendanceAuditLog,
    TeacherDailyAttendance, TimetablePeriodSetting, Department, ProxyAttendanceTransfer,
    TeacherAttendanceSettings, TeacherAttendanceAuditLog, UniversitySettings, TeacherEditRequest,
    TeacherFeedback, FacultyComplaint, ComplaintVote
)
from schedule_service import generate_daily_schedule, calculate_student_attendance
from auth.routes import save_base64_image
from teacher_attendance.routes import recalculate_daily_status, get_or_create_settings, parse_time_str

main_bp = Blueprint('main', __name__)

UPLOAD_FOLDER = 'temp_uploads'
FACES_FOLDER = os.path.join(UPLOAD_FOLDER, 'faces')
GROUP_PHOTOS_FOLDER = os.path.join(UPLOAD_FOLDER, 'group_photos')

os.makedirs(FACES_FOLDER, exist_ok=True)
os.makedirs(GROUP_PHOTOS_FOLDER, exist_ok=True)

# ==============================================================================
# SECTION 1: ROLE-BASED ACCESS CONTROL & ADMIN DATA SCOPING
# ==============================================================================
def admin_required(f):
    """Decorator to ensure only logged in admins can access admin routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash("Admin access is required to view this page.", "danger")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

# Helper queries to filter by admin multi-tenancy (allowing pre-populated global items where admin_id is null)
def get_admin_classes():
    return Class.query.order_by(Class.id.asc()).all()

def get_admin_teachers(approved_only=True):
    query = Teacher.query.filter((Teacher.admin_id == None) | (Teacher.admin_id == current_user.id))
    if approved_only:
        query = query.filter(Teacher.status != 'Pending')
    return query.all()

def get_admin_subjects():
    return Subject.query.filter((Subject.admin_id == None) | (Subject.admin_id == current_user.id)).all()

def get_admin_students():
    # Retrieve students managed by this admin or unassigned
    class_ids = [c.id for c in get_admin_classes()]
    if class_ids:
        return Student.query.filter((Student.class_id.in_(class_ids)) | (Student.class_id == None)).order_by(Student.id.asc()).all()
    return Student.query.all()

@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('main.dashboard'))
        elif current_user.role == 'teacher':
            return redirect(url_for('teacher.dashboard'))
        elif current_user.role == 'student':
            return redirect(url_for('student.dashboard'))

    # Load resources needed for unified auth SPA inside landing page
    from flask import session, current_app
    classes = Class.query.all()
    all_subject_names = [s[0] for s in db.session.query(Subject.name).distinct().order_by(Subject.name).all()]
    google_enabled = bool(current_app.config.get('GOOGLE_CLIENT_ID') and 
                          current_app.config.get('GOOGLE_CLIENT_SECRET'))
    google_data = session.get('google_signup_data')
    
    state = request.args.get('state', 'welcome')
    email = request.args.get('email', '')
    
    all_departments = Department.query.order_by(Department.name.asc()).all()
    
    return render_template(
        'landing.html', 
        classes=classes, 
        all_departments=all_departments,
        all_subject_names=all_subject_names,
        google_enabled=google_enabled, 
        state=state, 
        google_data=google_data,
        email=email
    )

def get_retention_risk_students():
    """
    Identifies students absent for 5 CONSECUTIVE ACADEMIC DAYS.
    Rules:
    - Academic days are distinct dates where completed attendance sessions occurred.
    - Does NOT flag students before at least 5 academic days exist.
    - Automatically ignores holidays & non-class weekends.
    - Immediately removes students who attend even one class session.
    """
    from sqlalchemy.orm import joinedload

    admin_students = get_admin_students()
    admin_student_ids = [s.id for s in admin_students]

    if not admin_student_ids:
        return []

    # 1. Fetch distinct academic dates of COMPLETED sessions in descending order
    distinct_dates = db.session.query(AttendanceSession.date)\
        .filter(AttendanceSession.status == 'COMPLETED')\
        .distinct().order_by(AttendanceSession.date.desc()).all()
    
    if len(distinct_dates) < 5:
        return []

    last_5_academic_days = [d[0] for d in distinct_dates[:5]]

    # 2. Find sessions on those dates
    session_ids = [s.id for s in AttendanceSession.query.filter(
        AttendanceSession.date.in_(last_5_academic_days),
        AttendanceSession.status == 'COMPLETED'
    ).all()]

    if not session_ids:
        return []

    # 3. Get student IDs who were PRESENT for ANY completed class in those 5 academic days
    present_student_ids = db.session.query(AttendanceRecord.student_id).filter(
        AttendanceRecord.session_id.in_(session_ids),
        AttendanceRecord.status == 'PRESENT',
        AttendanceRecord.student_id.in_(admin_student_ids)
    ).distinct().all()
    present_student_ids = [s[0] for s in present_student_ids]

    # 4. Risk students = enrolled students with NO 'PRESENT' records in last 5 academic days
    risk_students = Student.query.filter(
        Student.id.in_(admin_student_ids),
        Student.id.notin_(present_student_ids)
    ).all()

    return risk_students

def compute_emergency_proxy_desk(target_date=None):
    """
    Computes emergency class workload transfer for absent faculty,
    calculates free present teachers for each affected period,
    and supports intelligent period splitting and cancellation.
    Faculty with 'Pending' attendance (within check-in window) are NOT treated as absent.
    """
    if target_date is None:
        target_date = date.today()
    
    # 1. Generate or fetch daily schedules
    generate_daily_schedule(target_date)
    
    # 2. Get all teachers and their attendance for target_date
    all_teachers = Teacher.query.order_by(Teacher.name).all()
    today_recs = TeacherDailyAttendance.query.filter_by(attendance_date=target_date).all()
    settings = get_or_create_settings()
    
    for r in today_recs:
        recalculate_daily_status(r, settings)
    
    rec_by_teacher = {r.teacher_id: r for r in today_recs}
    
    # Fetch approved leaves on target_date
    approved_leaves = TeacherLeave.query.filter(
        TeacherLeave.status.in_(['APPROVED', 'Approved']),
        TeacherLeave.date_from <= target_date,
        TeacherLeave.date_to >= target_date
    ).all()
    leave_teacher_ids = {l.teacher_id for l in approved_leaves}

    now = datetime.now()
    today = date.today()
    morn_deadline = parse_time_str(settings.morning_deadline) or time(9, 0)
    grace_mins = settings.grace_period_mins if settings.grace_period_mins is not None else 30
    cutoff_dt = datetime.combine(today, morn_deadline) + timedelta(minutes=grace_mins)
    cutoff_time = cutoff_dt.time()
    
    absent_teachers = []
    present_teachers = []
    
    for t in all_teachers:
        rec = rec_by_teacher.get(t.id)
        if rec:
            st = rec.status
        else:
            if t.id in leave_teacher_ids:
                st = 'Approved Leave'
            elif target_date < today or (target_date == today and now.time() > cutoff_time):
                st = 'Absent'
            else:
                st = 'Pending'
        
        if st in ('Present', 'Half Day'):
            present_teachers.append(t)
        elif st in ('Absent', 'Approved Leave'):
            is_uninformed = False
            if st == 'Absent':
                is_uninformed = rec.is_uninformed_absence if rec else (t.id not in leave_teacher_ids)
                late_st = rec.late_status if rec else ('Uninformed Absent' if is_uninformed else 'Informed Absent')
            else:
                is_uninformed = False
                late_st = 'Approved Leave'

            absent_teachers.append({
                'teacher': t,
                'record': rec,
                'status': st,
                'is_uninformed': is_uninformed,
                'late_status': late_st
            })
        # 'Pending', 'Holiday', 'Weekend' are not counted as absent
            
    absent_teacher_ids = {a['teacher'].id for a in absent_teachers}
    
    # 3. Find all daily schedules for today
    day_schedules = DailySchedule.query.join(Timetable).filter(
        DailySchedule.date == target_date,
        Timetable.slot_type == 'CLASS'
    ).order_by(Timetable.period_no, Timetable.start_time).all()
    
    # Build schedule map for present teachers: which periods they are busy
    present_teacher_busy_slots = {pt.id: set() for pt in present_teachers}
    total_slots_in_day = set()
    
    for ds in day_schedules:
        tt = ds.timetable
        if tt.period_no:
            total_slots_in_day.add(tt.period_no)
        # If assigned original teacher is present and slot not cancelled
        if tt.teacher_id in present_teacher_busy_slots and not ds.is_cancelled:
            if ds.substitute_teacher_id != tt.teacher_id:
                present_teacher_busy_slots[tt.teacher_id].add(tt.period_no)
        # If substitute teacher is present and slot active
        if ds.substitute_teacher_id in present_teacher_busy_slots and not ds.is_cancelled:
            present_teacher_busy_slots[ds.substitute_teacher_id].add(tt.period_no)
            
    # For each affected slot of absent teachers, find free present teachers
    affected_slots = []
    for ds in day_schedules:
        tt = ds.timetable
        is_teacher_absent = tt.teacher_id in absent_teacher_ids
        if is_teacher_absent or ds.is_cancelled or ds.is_proxy or ds.resolved_status in ('TEACHER_ON_LEAVE', 'SUBSTITUTE_ASSIGNED', 'CANCELLED'):
            orig_teacher = tt.teacher_assigned
            orig_rec = rec_by_teacher.get(tt.teacher_id) if tt.teacher_id else None
            
            available_teachers = []
            slot_period = tt.period_no or 1
            
            for pt in present_teachers:
                if pt.id == tt.teacher_id:
                    continue
                is_busy = slot_period in present_teacher_busy_slots.get(pt.id, set())
                free_period_count = len(total_slots_in_day - present_teacher_busy_slots.get(pt.id, set()))
                
                if not is_busy:
                    available_teachers.append({
                        'id': pt.id,
                        'name': pt.name,
                        'department': pt.department or 'General',
                        'free_periods_count': free_period_count,
                        'primary_subject': pt.primary_subject or ''
                    })
            
            available_teachers.sort(key=lambda x: x['free_periods_count'], reverse=True)
            
            affected_slots.append({
                'daily_schedule_id': ds.id,
                'timetable_id': tt.id,
                'period_no': tt.period_no,
                'start_time': tt.start_time,
                'end_time': tt.end_time,
                'class_name': tt.class_assigned.name if tt.class_assigned else 'N/A',
                'class_id': tt.class_id,
                'subject_name': tt.subject_assigned.name if tt.subject_assigned else (tt.custom_title or 'N/A'),
                'subject_code': tt.subject_assigned.code if tt.subject_assigned and tt.subject_assigned.code else '',
                'original_teacher_id': tt.teacher_id,
                'original_teacher_name': orig_teacher.name if orig_teacher else 'Unassigned',
                'original_teacher_absent_status': orig_rec.late_status if orig_rec else ('Approved Leave' if tt.teacher_id in leave_teacher_ids else 'Absent'),
                'is_uninformed': orig_rec.is_uninformed_absence if orig_rec else (tt.teacher_id not in leave_teacher_ids),
                'is_cancelled': ds.is_cancelled or ds.resolved_status == 'CANCELLED',
                'cancellation_reason': ds.cancellation_reason,
                'is_proxy': ds.is_proxy or ds.resolved_status == 'SUBSTITUTE_ASSIGNED',
                'substitute_teacher_id': ds.substitute_teacher_id,
                'substitute_teacher_name': ds.substitute_teacher.name if ds.substitute_teacher else None,
                'available_teachers': available_teachers
            })
            
    pending_slots = [s for s in affected_slots if not s['is_proxy'] and not s['is_cancelled']]

    return {
        'absent_teachers': absent_teachers,
        'uninformed_absent_count': sum(1 for a in absent_teachers if a['is_uninformed']),
        'total_absent_count': len(absent_teachers),
        'present_teachers_count': len(present_teachers),
        'affected_slots': affected_slots,
        'pending_slots': pending_slots,
        'unresolved_slots_count': len(pending_slots),
        'proxy_slots_count': sum(1 for s in affected_slots if s['is_proxy']),
        'cancelled_slots_count': sum(1 for s in affected_slots if s['is_cancelled'])
    }

@main_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    today = get_current_date()
    # Trigger daily schedule generation for today
    generate_daily_schedule(today)

    students_list = get_admin_students()
    unclassified_students = Student.query.filter(Student.class_id == None).all()
    students_list = list(set(students_list + unclassified_students))

    subjects = get_admin_subjects()
    classes = get_admin_classes()

    total_students = len(students_list)
    subjects_managed = len(subjects)
    
    subject_names = [sub.name for sub in subjects]
    attendance_percentages = []

    for subject in subjects:
        # Subject attendance percentage across completed sessions
        completed_sessions = AttendanceSession.query.filter_by(
            subject_id=subject.id,
            status='COMPLETED'
        ).all()
        comp_ids = [s.id for s in completed_sessions]
        if comp_ids:
            total_present = AttendanceRecord.query.filter(
                AttendanceRecord.session_id.in_(comp_ids),
                AttendanceRecord.status == 'PRESENT'
            ).count()
            students_in_class = Student.query.filter_by(class_id=subject.class_id).count()
            denom = len(comp_ids) * (students_in_class or 1)
            pct = round((total_present / denom * 100), 2) if denom > 0 else 0.0
            attendance_percentages.append(pct)
        else:
            attendance_percentages.append(0.0)

    # Calculate overall attendance dynamically for each student using calculate_student_attendance
    for student in students_list:
        stats = calculate_student_attendance(student.id)
        student.overall_attendance = stats['percentage']

    # Build Class-wise Interactive Metrics Map
    class_details = {}
    total_class_pct_sum = 0
    valid_class_count = 0

    for c in classes:
        c_students = [s for s in students_list if s.class_id == c.id]
        enroll_count = len(c_students)
        
        c_subjects = Subject.query.filter_by(class_id=c.id).all()
        class_teacher_name = c.class_teacher.name if c.class_teacher else "No Class Teacher Assigned"
        teachers_list = list(set([sub.teacher.name for sub in c_subjects if sub.teacher]))
        teachers_str = ", ".join(teachers_list) if teachers_list else "None"
        faculty_display = f"Class Teacher: {class_teacher_name} | Subjects: {teachers_str}"
        
        pct_sum = 0
        st_count = 0
        for student in c_students:
            st_stats = calculate_student_attendance(student.id)
            if st_stats['completed_sessions'] > 0:
                pct_sum += st_stats['percentage']
                st_count += 1

        avg_attendance = round((pct_sum / st_count), 2) if st_count > 0 else 0.0
        if st_count > 0:
            total_class_pct_sum += avg_attendance
            valid_class_count += 1
        
        class_details[f"class-{c.id}"] = {
            "name": c.name,
            "enrolled": enroll_count,
            "faculty": faculty_display,
            "avg_attendance": f"{avg_attendance}%"
        }

    # Add unclassified metrics
    class_details["class-None"] = {
        "name": "Awaiting Class",
        "enrolled": len(unclassified_students),
        "faculty": "Pending Assignment",
        "avg_attendance": "0.0%"
    }

    overall_avg = round((total_class_pct_sum / valid_class_count), 2) if valid_class_count > 0 else 0.0
    
    class_details["all"] = {
        "name": "All Classes",
        "enrolled": total_students,
        "faculty": "",
        "avg_attendance": f"{overall_avg}%"
    }

    retention_risk_students = get_retention_risk_students()

    # Uncovered leave slots for admin dashboard widget
    uncovered_leave_slots = DailySchedule.query.filter_by(
        date=today,
        resolved_status='TEACHER_ON_LEAVE'
    ).all()

    # --- Live Enterprise Dashboard Metrics ---
    # 1. Faculty Check-In Pulse & Presence/Absence Breakdown
    all_admin_teachers = get_admin_teachers()
    total_teachers_count = len(all_admin_teachers)
    today_teacher_recs = TeacherDailyAttendance.query.filter_by(attendance_date=today).all()
    teacher_rec_map = {r.teacher_id: r for r in today_teacher_recs}
    
    today_day_name = today.strftime('%A')
    today_timetable_slots = Timetable.query.filter_by(day_of_week=today_day_name).order_by(Timetable.start_time).all()
    teacher_periods_map = {}
    for slot in today_timetable_slots:
        if slot.slot_type == 'CLASS':
            teacher_periods_map.setdefault(slot.teacher_id, []).append(slot)

    faculty_status_breakdown = {
        'total': total_teachers_count,
        'present_total': 0,
        'present_ontime': 0,
        'present_late': 0,
        'informed_leave': 0,
        'uninformed_absent': 0,
        'faculty_list': []
    }

    recent_teacher_checkins = []

    for t in all_admin_teachers:
        rec = teacher_rec_map.get(t.id)
        leave = TeacherLeave.query.filter(
            TeacherLeave.teacher_id == t.id,
            TeacherLeave.status == 'APPROVED',
            TeacherLeave.date_from <= today,
            TeacherLeave.date_to >= today
        ).first()

        scheduled_slots = teacher_periods_map.get(t.id, [])
        status_category = 'UNINFORMED_ABSENT'
        status_label = 'Uninformed Absent'
        badge_class = 'danger'
        arrival_text = 'Not Checked In'
        late_mins = 0

        if rec and rec.check_in_at:
            if rec.late_status == 'Late':
                status_category = 'PRESENT_LATE'
                late_mins = rec.late_minutes or 0
                status_label = f"Present ({late_mins}m Late)"
                badge_class = 'warning text-dark'
                arrival_text = rec.check_in_at.strftime('%I:%M %p')
                faculty_status_breakdown['present_late'] += 1
                faculty_status_breakdown['present_total'] += 1
            else:
                status_category = 'PRESENT_ONTIME'
                status_label = "Present (On Time)"
                badge_class = 'success'
                arrival_text = rec.check_in_at.strftime('%I:%M %p')
                faculty_status_breakdown['present_ontime'] += 1
                faculty_status_breakdown['present_total'] += 1

            recent_teacher_checkins.append({
                'teacher': t,
                'check_in_time': rec.check_in_at.strftime('%I:%M %p'),
                'late_status': rec.late_status or 'On Time',
                'photo': t.image_filename,
                'face_verified': rec.check_in_face_verified
            })
        elif leave or (rec and (rec.informed_admin or rec.late_status in ('Leave Applied', 'Informed Absent'))):
            status_category = 'INFORMED_LEAVE'
            status_label = f"On Leave ({leave.leave_type if leave else 'Informed'})"
            badge_class = 'info text-dark'
            arrival_text = f"Leave: {leave.reason if leave else (rec.absence_reason if rec else 'Informed Admin')}"
            faculty_status_breakdown['informed_leave'] += 1
        else:
            status_category = 'UNINFORMED_ABSENT'
            status_label = "Uninformed Absent"
            badge_class = 'danger'
            arrival_text = "No Check-in (> 9:30 AM Cutoff)"
            faculty_status_breakdown['uninformed_absent'] += 1

        faculty_status_breakdown['faculty_list'].append({
            'teacher': t,
            'rec': rec,
            'leave': leave,
            'status_category': status_category,
            'status_label': status_label,
            'badge_class': badge_class,
            'arrival_text': arrival_text,
            'late_minutes': late_mins,
            'scheduled_periods_count': len(scheduled_slots),
            'scheduled_slots': scheduled_slots,
            'face_verified': rec.check_in_face_verified if rec else False,
            'photo': t.image_filename
        })

    teachers_checked_in_count = faculty_status_breakdown['present_total']
    teachers_late_count = faculty_status_breakdown['present_late']
    teachers_ontime_count = faculty_status_breakdown['present_ontime']
    teachers_absent_count = faculty_status_breakdown['informed_leave'] + faculty_status_breakdown['uninformed_absent']
    faculty_present_pct = round((teachers_checked_in_count / total_teachers_count * 100), 1) if total_teachers_count > 0 else 0.0

    # 2. Defaulters Count (< 75% attendance)
    defaulters_count = sum(1 for s in students_list if getattr(s, 'overall_attendance', 100) < 75.0)

    # 3. Period settings & Active Period
    period_settings = get_or_create_period_settings()
    now_time_str = datetime.now().strftime('%H:%M')
    active_period_no = None
    for ps in period_settings:
        if not ps.is_lunch and ps.start_time <= now_time_str <= ps.end_time:
            active_period_no = ps.period_no
            break

    # 4. Total Student Attendances Across Today's Classes
    today_sessions = AttendanceSession.query.filter_by(date=today, status='COMPLETED').all()
    today_session_ids = [s.id for s in today_sessions]
    
    today_records_present = 0
    today_records_absent = 0
    today_unique_present_count = 0
    if today_session_ids:
        today_records_present = AttendanceRecord.query.filter(
            AttendanceRecord.session_id.in_(today_session_ids),
            AttendanceRecord.status == 'PRESENT'
        ).count()
        today_records_absent = AttendanceRecord.query.filter(
            AttendanceRecord.session_id.in_(today_session_ids),
            AttendanceRecord.status == 'ABSENT'
        ).count()
        present_recs = AttendanceRecord.query.filter(
            AttendanceRecord.session_id.in_(today_session_ids),
            AttendanceRecord.status == 'PRESENT'
        ).all()
        today_unique_present_count = len({r.student_id for r in present_recs})

    today_total_attendances = today_records_present + today_records_absent
    today_attendance_pct = round((today_records_present / today_total_attendances * 100), 1) if today_total_attendances > 0 else 0.0
    today_scheduled_classes_count = len(today_timetable_slots)
    today_completed_classes_count = len(today_sessions)

    # 5. Automated Emergency Proxy Allocation & Class Cancellation Desk
    emergency_desk = compute_emergency_proxy_desk(today)

    return render_template(
        'dashboard.html',
        student_count=total_students,
        subjects_managed=subjects_managed,
        students=students_list,
        subject_names=subject_names,
        attendance_percentages=attendance_percentages,
        retention_risk_students=retention_risk_students,
        uncovered_leave_slots=uncovered_leave_slots,
        classes=classes,
        class_details=class_details,
        overall_avg=overall_avg,
        all_teachers_str="",
        total_teachers_count=total_teachers_count,
        teachers_checked_in_count=teachers_checked_in_count,
        teachers_late_count=teachers_late_count,
        teachers_ontime_count=teachers_ontime_count,
        teachers_absent_count=teachers_absent_count,
        faculty_present_pct=faculty_present_pct,
        faculty_status_breakdown=faculty_status_breakdown,
        recent_teacher_checkins=recent_teacher_checkins,
        defaulters_count=defaulters_count,
        today_day_name=today_day_name,
        today_timetable_slots=today_timetable_slots,
        period_settings=period_settings,
        active_period_no=active_period_no,
        today_records_present=today_records_present,
        today_records_absent=today_records_absent,
        today_total_attendances=today_total_attendances,
        today_attendance_pct=today_attendance_pct,
        today_unique_present_count=today_unique_present_count,
        today_scheduled_classes_count=today_scheduled_classes_count,
        today_completed_classes_count=today_completed_classes_count,
        today_sessions_count=today_completed_classes_count,
        teachers=all_admin_teachers,
        all_departments=Department.query.order_by(Department.name.asc()).all(),
        emergency_desk=emergency_desk
    )

# --- EMERGENCY PROXY & CLASS CANCELLATION ACTIONS ---

@main_bp.route('/admin/assign_slot_proxy', methods=['POST'])
@login_required
def assign_slot_proxy():
    if current_user.role != 'admin':
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    
    daily_schedule_id = request.form.get('daily_schedule_id', type=int)
    proxy_teacher_id = request.form.get('proxy_teacher_id', type=int)
    
    ds = DailySchedule.query.get_or_404(daily_schedule_id)
    proxy_teacher = Teacher.query.get_or_404(proxy_teacher_id)
    tt = ds.timetable
    
    ds.substitute_teacher_id = proxy_teacher.id
    ds.is_proxy = True
    ds.is_cancelled = False
    ds.cancellation_reason = None
    ds.resolved_status = 'SUBSTITUTE_ASSIGNED'
    ds.proxy_assigned_by_admin_id = current_user.id
    ds.proxy_assigned_at = datetime.now()
    
    # Sync with ProxyAttendanceTransfer
    transfer = ProxyAttendanceTransfer.query.filter_by(
        timetable_id=tt.id,
        date=ds.date
    ).first()
    if not transfer:
        transfer = ProxyAttendanceTransfer(
            substitute_teacher_id=proxy_teacher.id,
            original_teacher_id=tt.teacher_id,
            timetable_id=tt.id,
            class_id=tt.class_id,
            subject_id=tt.subject_id,
            date=ds.date,
            time_slot=f"{tt.start_time} - {tt.end_time}",
            present_rolls="",
            status='PENDING'
        )
        db.session.add(transfer)
    else:
        transfer.substitute_teacher_id = proxy_teacher.id
        transfer.original_teacher_id = tt.teacher_id
        
    db.session.commit()
    class_name = tt.class_assigned.name if tt.class_assigned else ''
    subj_name = tt.subject_assigned.name if tt.subject_assigned else ''
    flash(f"✓ Proxy Assigned: Prof. {proxy_teacher.name} assigned to Period {tt.period_no or ''} ({class_name} - {subj_name}).", "success")
    return redirect(request.referrer or url_for('main.dashboard'))

@main_bp.route('/admin/cancel_class_slot', methods=['POST'])
@login_required
def cancel_class_slot():
    if current_user.role != 'admin':
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    
    daily_schedule_id = request.form.get('daily_schedule_id', type=int)
    reason = request.form.get('reason', 'Faculty unavailable & No proxy available').strip()
    
    ds = DailySchedule.query.get_or_404(daily_schedule_id)
    tt = ds.timetable
    
    ds.is_cancelled = True
    ds.cancellation_reason = reason or "Faculty unavailable"
    ds.resolved_status = 'CANCELLED'
    ds.substitute_teacher_id = None
    ds.is_proxy = False
    
    db.session.commit()
    class_name = tt.class_assigned.name if tt.class_assigned else ''
    subj_name = tt.subject_assigned.name if tt.subject_assigned else ''
    flash(f"🚫 Period {tt.period_no or ''} ({class_name} - {subj_name}) marked as CANCELLED. Notice is now active on the Student Dashboard.", "warning")
    return redirect(request.referrer or url_for('main.dashboard'))

@main_bp.route('/admin/restore_class_slot', methods=['POST'])
@login_required
def restore_class_slot():
    if current_user.role != 'admin':
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    
    daily_schedule_id = request.form.get('daily_schedule_id', type=int)
    ds = DailySchedule.query.get_or_404(daily_schedule_id)
    
    ds.is_cancelled = False
    ds.cancellation_reason = None
    ds.is_proxy = False
    ds.substitute_teacher_id = None
    ds.resolved_status = 'SCHEDULED'
    
    db.session.commit()
    flash("✓ Slot restored to standard schedule.", "info")
    return redirect(request.referrer or url_for('main.dashboard'))

@main_bp.route('/admin/manual_faculty_override', methods=['POST'])
@login_required
def manual_faculty_override():
    if current_user.role != 'admin':
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    
    teacher_id = request.form.get('teacher_id', type=int)
    target_date_str = request.form.get('date', date.today().strftime('%Y-%m-%d'))
    new_status = request.form.get('status', 'Present').strip()
    reason = request.form.get('reason', 'Admin manual override').strip()
    
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        target_date = date.today()
        
    rec = TeacherDailyAttendance.query.filter_by(teacher_id=teacher_id, attendance_date=target_date).first()
    if not rec:
        rec = TeacherDailyAttendance(teacher_id=teacher_id, attendance_date=target_date)
        db.session.add(rec)
        
    old_status = rec.status
    rec.status = new_status
    rec.is_admin_overridden = True
    rec.informed_admin = True
    rec.absence_reason = reason
    if new_status == 'Present':
        rec.is_uninformed_absence = False
        rec.late_status = 'On Time'
    elif new_status in ('Approved Leave', 'Official Duty'):
        rec.is_uninformed_absence = False
        rec.late_status = 'Approved Leave'
    else:
        rec.late_status = 'Admin Overridden Absent'
        
    # Log in audit
    audit = TeacherAttendanceAuditLog(
        teacher_daily_attendance_id=rec.id,
        teacher_id=teacher_id,
        attendance_date=target_date,
        modified_by_admin_id=current_user.id,
        old_status=old_status,
        new_status=new_status,
        reason=reason
    )
    db.session.add(audit)
    db.session.commit()
    
    # Regenerate daily lecture schedule for target date
    try:
        generate_daily_schedule(target_date)
    except Exception as sched_err:
        print(f"[Manual Override] Error regenerating daily schedule: {sched_err}")
    
    flash(f"✓ Faculty status updated to '{new_status}' successfully and schedule synchronized.", "success")
    return redirect(request.referrer or url_for('main.dashboard'))

@main_bp.route('/admin/export_exam_eligibility_report')
@admin_required
def export_exam_eligibility_report():
    threshold_str = request.args.get('threshold', '50.0')
    condition = request.args.get('condition', 'below')
    class_id_filter = request.args.get('class_id', 'all')
    
    try:
        threshold = float(threshold_str)
    except ValueError:
        threshold = 50.0

    all_students = get_admin_students()
    all_classes = get_admin_classes()

    for s in all_students:
        stats = calculate_student_attendance(s.id)
        s.overall_attendance = stats.get('percentage', 0.0)
        s.present_sessions = stats.get('attended', 0)
        s.total_sessions = stats.get('completed_sessions', 0)

    # Filter students
    matching_students = []
    for s in all_students:
        pct = getattr(s, 'overall_attendance', 0.0)
        if condition == 'below':
            is_match = (pct < threshold)
        else:
            is_match = (pct >= threshold)

        if class_id_filter != 'all':
            try:
                cid = int(class_id_filter)
                if s.class_id != cid:
                    is_match = False
            except ValueError:
                pass

        if is_match:
            matching_students.append(s)

    # Group by class
    class_groups = []
    for c in all_classes:
        if class_id_filter != 'all' and str(c.id) != str(class_id_filter):
            continue
        c_students = [s for s in matching_students if s.class_id == c.id]
        total_enrolled = Student.query.filter_by(class_id=c.id).count()
        class_groups.append({
            'class': c,
            'name': c.name,
            'department': c.department or 'General',
            'students': c_students,
            'total_enrolled': total_enrolled,
            'matching_count': len(c_students)
        })

    # Unclassified
    if class_id_filter in ['all', 'unclassified', 'None']:
        unclassified_students = [s for s in matching_students if not s.class_id]
        if unclassified_students:
            class_groups.append({
                'class': None,
                'name': 'Pending / Unclassified',
                'department': 'Unassigned',
                'students': unclassified_students,
                'total_enrolled': len(unclassified_students),
                'matching_count': len(unclassified_students)
            })

    return render_template(
        'export_exam_eligibility.html',
        threshold=threshold,
        condition=condition,
        class_id_filter=class_id_filter,
        matching_students=matching_students,
        total_students_count=len(all_students),
        class_groups=class_groups,
        generated_at=datetime.now().strftime('%d %B %Y, %I:%M %p')
    )

@main_bp.route('/api/ai_copilot/query', methods=['POST'])
@login_required
def ai_copilot_query():
    from ai_copilot_service import ask_ai_copilot
    data = request.get_json(silent=True) or {}
    query_text = data.get('query', '').strip()
    user_role = getattr(current_user, 'role', 'admin')
    user_id = getattr(current_user, 'id', None)
    
    result = ask_ai_copilot(query_text=query_text, user_role=user_role, user_id=user_id)
    return jsonify(result)


# ==============================================================================
# ROLE-BASED LIVE NOTIFICATIONS POLLING ENDPOINT (FOR APP & PWA)
# ==============================================================================
@main_bp.route('/api/user/notifications', methods=['GET'])
@login_required
def get_user_notifications():
    """
    Returns live, real-time unread notifications strictly scoped to the user's role:
    - ADMIN: Pending teacher/student registrations, edit approvals, discrepancy requests.
    - TEACHER: Assigned proxy duties, approved leaves, student change requests, admin notices.
    - STUDENT: College holidays/off days, class notices, approved modifications, attendance alerts.
    """
    notifications = []
    role = getattr(current_user, 'role', 'user')

    try:
        if role == 'admin':
            # 1. New Pending Teacher Registrations
            pending_teachers = Teacher.query.filter_by(status='Pending').count()
            if pending_teachers > 0:
                notifications.append({
                    'id': f'admin_pending_teachers_{pending_teachers}',
                    'title': 'Faculty Approval Required',
                    'body': f'{pending_teachers} faculty member(s) registered and awaiting your approval.',
                    'url': url_for('main.admin_approvals'),
                    'tag': 'admin_teachers'
                })

            # 2. Pending Student Profile Edit Requests
            pending_edits = StudentEditRequest.query.filter_by(status='PENDING').count()
            if pending_edits > 0:
                notifications.append({
                    'id': f'admin_student_edits_{pending_edits}',
                    'title': 'Student Modification Request',
                    'body': f'{pending_edits} student modification request(s) waiting for review.',
                    'url': url_for('main.admin_approvals'),
                    'tag': 'admin_edits'
                })

            # 3. Pending Discrepancy Requests
            pending_discrepancies = CorrectionRequest.query.filter_by(status='Pending').count()
            if pending_discrepancies > 0:
                notifications.append({
                    'id': f'admin_discrepancies_{pending_discrepancies}',
                    'title': 'Attendance Correction Request',
                    'body': f'{pending_discrepancies} attendance discrepancy request(s) need attention.',
                    'url': url_for('main.admin_approvals'),
                    'tag': 'admin_discrepancies'
                })

        elif role == 'teacher':
            teacher = getattr(current_user, 'teacher_profile', None)
            if teacher:
                # 1. Proxy duty assignments for today (Only notify if NOT yet taken / shared)
                from models import TeacherLeave, DailySchedule, Timetable, ProxyAttendanceTransfer
                today = get_current_date()
                day_name = today.strftime('%A')

                # Find all shared/completed proxy transfers for current teacher today
                completed_shared_keys = set(
                    (t.timetable_id, t.date) for t in ProxyAttendanceTransfer.query.filter_by(
                        substitute_teacher_id=teacher.id,
                        date=today
                    ).all() if t.status in ('SHARED', 'APPLIED') or bool(t.present_rolls)
                )

                pending_proxy_count = 0

                # A. Approved substitute leaves for today
                today_sub_leaves = TeacherLeave.query.filter_by(
                    substitute_teacher_id=teacher.id,
                    status='APPROVED'
                ).filter(TeacherLeave.date_from <= today, TeacherLeave.date_to >= today).all()

                for p_leave in today_sub_leaves:
                    if p_leave.teacher:
                        slots = Timetable.query.filter_by(teacher_id=p_leave.teacher.id, day_of_week=day_name, slot_type='CLASS').all()
                        for s in slots:
                            if (s.id, today) not in completed_shared_keys:
                                pending_proxy_count += 1

                # B. Direct emergency proxy allocations
                direct_proxies = DailySchedule.query.filter_by(
                    substitute_teacher_id=teacher.id,
                    date=today,
                    is_proxy=True
                ).all()
                for dp in direct_proxies:
                    if not dp.is_cancelled and (dp.timetable_id, today) not in completed_shared_keys:
                        pending_proxy_count += 1

                if pending_proxy_count > 0:
                    notifications.append({
                        'id': f'teacher_proxy_{today}_{pending_proxy_count}',
                        'title': 'Proxy Duty Assigned',
                        'body': f'You have {pending_proxy_count} pending proxy lecture duty scheduled for today.',
                        'url': url_for('teacher.proxy_classes'),
                        'tag': 'teacher_proxy'
                    })

                # 2. Institutional Notices for Teachers
                from models import TeacherReadNotice
                read_notice_ids = [r.announcement_id for r in TeacherReadNotice.query.filter_by(teacher_id=teacher.id).all()]
                unread_notices = ClassAnnouncement.query.filter(
                    ClassAnnouncement.target_role.in_(['TEACHERS', 'ALL']),
                    ~ClassAnnouncement.id.in_(read_notice_ids) if read_notice_ids else True
                ).order_by(ClassAnnouncement.created_at.desc()).limit(3).all()

                for notice in unread_notices:
                    notifications.append({
                        'id': f'teacher_notice_{notice.id}',
                        'title': f'Notice: {notice.title[:40]}',
                        'body': notice.content[:100] + ('...' if len(notice.content) > 100 else ''),
                        'url': url_for('teacher.announcements'),
                        'tag': f'notice_{notice.id}'
                    })

        elif role == 'student':
            student = getattr(current_user, 'student_profile', None)
            if student:
                # 1. Today Holiday / College Off Alert
                today = get_current_date()
                holiday_today = Holiday.query.filter_by(date=today).first()
                if holiday_today:
                    notifications.append({
                        'id': f'student_holiday_{today}',
                        'title': f'College Holiday: {holiday_today.name}',
                        'body': f'Today ({today.strftime("%A, %d %b")}) is declared as a holiday. Classes are off.',
                        'url': url_for('student.dashboard'),
                        'tag': f'holiday_{today}'
                    })

                # 2. Class Announcements & Notices
                from models import StudentReadNotice
                from datetime import datetime
                stu_reg_date = student.created_at or (student.user_account.created_at if (student.user_account and hasattr(student.user_account, 'created_at')) else None)
                stu_reg_cutoff = datetime.combine(stu_reg_date.date(), datetime.min.time()) if stu_reg_date else None

                read_notice_ids = [r.announcement_id for r in StudentReadNotice.query.filter_by(student_id=student.id).all()]
                unread_notices_q = ClassAnnouncement.query.filter(
                    (ClassAnnouncement.class_id == student.class_id) | (ClassAnnouncement.class_id == None),
                    ClassAnnouncement.target_role.in_(['STUDENTS', 'ALL']),
                    ~ClassAnnouncement.id.in_(read_notice_ids) if read_notice_ids else True
                )
                if stu_reg_cutoff:
                    unread_notices_q = unread_notices_q.filter(ClassAnnouncement.created_at >= stu_reg_cutoff)
                unread_notices = unread_notices_q.order_by(ClassAnnouncement.created_at.desc()).limit(3).all()

                for notice in unread_notices:
                    notifications.append({
                        'id': f'student_notice_{notice.id}',
                        'title': f'Notice: {notice.title[:40]}',
                        'body': notice.content[:100] + ('...' if len(notice.content) > 100 else ''),
                        'url': url_for('student.student_notices'),
                        'tag': f'notice_{notice.id}'
                    })

    except Exception as e:
        print(f"Error fetching notifications: {e}")

    return jsonify({
        'role': role,
        'notifications': notifications
    })



@main_bp.route('/manage_classes', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_classes():
    if request.method == 'POST':
        class_name = request.form.get('class_name', '').strip()
        department = request.form.get('department', '').strip()
        class_teacher_id = request.form.get('class_teacher_id')
        
        subject_names = request.form.getlist('subject_names')
        subject_teacher_ids = request.form.getlist('subject_teacher_ids')

        if not class_name:
            flash('Class name cannot be empty.', 'warning')
            return redirect(url_for('main.manage_classes'))

        c_teacher_id = int(class_teacher_id) if class_teacher_id and class_teacher_id.isdigit() else None
        target_class = Class.query.filter_by(name=class_name).first()
        is_new_class = False

        try:
            if not target_class:
                is_new_class = True
                target_class = Class(name=class_name, department=department or None, admin_id=current_user.id, class_teacher_id=c_teacher_id)
                db.session.add(target_class)
                db.session.flush()
            else:
                if department:
                    target_class.department = department
                if c_teacher_id is not None:
                    target_class.class_teacher_id = c_teacher_id

            subjects_added_count = 0
            for i, sub_name in enumerate(subject_names):
                sname = sub_name.strip()
                tid_str = subject_teacher_ids[i] if i < len(subject_teacher_ids) else ''
                tid = int(tid_str) if tid_str and tid_str.isdigit() else None

                if sname:
                    existing_sub = Subject.query.filter_by(name=sname, class_id=target_class.id).first()
                    if existing_sub:
                        existing_sub.teacher_id = tid
                    else:
                        new_sub = Subject(
                            name=sname,
                            class_id=target_class.id,
                            teacher_id=tid,
                            admin_id=current_user.id
                        )
                        db.session.add(new_sub)
                    subjects_added_count += 1

            db.session.commit()
            if is_new_class:
                flash(f"Class '{class_name}' created successfully with {subjects_added_count} subject assignment(s)!", 'success')
            else:
                flash(f"Class '{class_name}' updated successfully! Added/Updated {subjects_added_count} subject assignment(s).", 'success')
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating class: {e}", 'danger')

        return redirect(url_for('main.manage_classes'))

    classes = get_admin_classes()
    teachers = get_admin_teachers()
    all_departments = Department.query.order_by(Department.name.asc()).all()
    all_subject_names = [s[0] for s in db.session.query(Subject.name).distinct().order_by(Subject.name).all()]
    return render_template('manage_classes.html', classes=classes, teachers=teachers, all_departments=all_departments, all_subject_names=all_subject_names)

@main_bp.route('/admin/create_department', methods=['POST'])
@login_required
@admin_required
def create_department():
    dept_name = request.form.get('name', '').strip().upper()
    full_name = request.form.get('full_name', '').strip()
    code = request.form.get('code', '').strip().upper()

    if not dept_name:
        flash("Department Name/Code is required (e.g. CSE, AI/ML).", "warning")
        return redirect(url_for('main.manage_classes'))

    existing = Department.query.filter_by(name=dept_name).first()
    if existing:
        flash(f"Department '{dept_name}' already exists.", "info")
        return redirect(url_for('main.manage_classes'))

    try:
        new_dept = Department(
            name=dept_name,
            full_name=full_name or dept_name,
            code=code or dept_name,
            admin_id=current_user.id
        )
        db.session.add(new_dept)
        db.session.commit()
        flash(f"Department '{dept_name}' created successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error creating department: {e}", "danger")

    return redirect(url_for('main.manage_classes'))

@main_bp.route('/admin/delete_department/<int:dept_id>', methods=['POST'])
@login_required
@admin_required
def delete_department(dept_id):
    dept = Department.query.get_or_404(dept_id)
    dept_name = dept.name
    try:
        db.session.delete(dept)
        db.session.commit()
        flash(f"Department '{dept_name}' deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting department: {e}", "danger")

    return redirect(url_for('main.manage_classes'))

@main_bp.route('/admin/edit_student/<int:student_id>', methods=['POST'])
@login_required
@admin_required
def edit_student(student_id):
    student = Student.query.get_or_404(student_id)
    name = request.form.get('name', '').strip()
    roll_no = request.form.get('roll_no', '').strip()
    enrollment_no = request.form.get('enrollment_no', '').strip()
    mobile = request.form.get('mobile', '').strip()
    class_id = request.form.get('class_id')
    department = request.form.get('department', '').strip()

    if not name or not enrollment_no:
        flash("Student Name and Enrollment Number are required.", "warning")
        return redirect(request.referrer or url_for('main.dashboard'))

    try:
        student.name = name
        if roll_no:
            student.roll_no = roll_no
            student.roll_number = roll_no
        student.enrollment_no = enrollment_no
        if mobile:
            student.mobile = mobile
        if class_id and class_id.isdigit():
            student.class_id = int(class_id)
        elif class_id == '':
            student.class_id = None
        if department:
            student.department = department
        elif student.class_id:
            c = Class.query.get(student.class_id)
            if c and c.department:
                student.department = c.department

        db.session.commit()
        flash(f"Student '{student.name}' profile updated successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating student: {e}", "danger")

    return redirect(request.referrer or url_for('main.dashboard'))

@main_bp.route('/assign_class_teacher/<int:class_id>', methods=['POST'])
@login_required
@admin_required
def assign_class_teacher(class_id):
    class_item = Class.query.get_or_404(class_id)
    
    teacher_id = request.form.get('class_teacher_id')
    if teacher_id:
        class_item.class_teacher_id = int(teacher_id)
    else:
        class_item.class_teacher_id = None
        
    db.session.commit()
    flash(f"Class Teacher updated successfully for '{class_item.name}'!", "success")
    return redirect(url_for('main.manage_classes'))

@main_bp.route('/delete_class/<int:class_id>', methods=['POST'])
@login_required
@admin_required
def delete_class(class_id):
    class_to_delete = Class.query.get_or_404(class_id)
        
    try:
        from models import Timetable, DailySchedule, ProxyAttendanceTransfer, AttendanceSession, AttendanceRecord, AttendanceAuditLog, ClassAnnouncement, StudentEditRequest, Student
        
        # 1. Clean proxy transfers for this class
        ProxyAttendanceTransfer.query.filter_by(class_id=class_id).delete(synchronize_session=False)

        # 2. Clean timetable slots & daily schedules for this class
        tt_ids = [t.id for t in Timetable.query.filter_by(class_id=class_id).all()]
        if tt_ids:
            DailySchedule.query.filter(DailySchedule.timetable_id.in_(tt_ids)).delete(synchronize_session=False)
            Timetable.query.filter_by(class_id=class_id).delete(synchronize_session=False)

        # 3. Clean attendance audit logs, records and sessions for students in this class
        st_ids = [st.id for st in Student.query.filter_by(class_id=class_id).all()]
        if st_ids:
            AttendanceAuditLog.query.filter(AttendanceAuditLog.student_id.in_(st_ids)).delete(synchronize_session=False)

        sess_ids = [s.id for s in AttendanceSession.query.filter_by(class_id=class_id).all()]
        if sess_ids:
            AttendanceRecord.query.filter(AttendanceRecord.session_id.in_(sess_ids)).delete(synchronize_session=False)
            AttendanceSession.query.filter_by(class_id=class_id).delete(synchronize_session=False)

        # 4. Clean class announcements & student edit requests
        ClassAnnouncement.query.filter_by(class_id=class_id).delete(synchronize_session=False)
        StudentEditRequest.query.filter_by(new_class_id=class_id).delete(synchronize_session=False)

        db.session.delete(class_to_delete)
        db.session.commit()
        flash(f"Class '{class_to_delete.name}' and all associated student/schedule data have been deleted successfully.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting class: {e}", 'danger')
    return redirect(url_for('main.manage_classes'))

@main_bp.route('/add_teacher', methods=['POST'])
@login_required
@admin_required
def add_teacher():
    teacher_name = request.form.get('teacher_name', '').strip()
    teacher_email = request.form.get('teacher_email', '').strip().lower()
    teacher_password = request.form.get('teacher_password', '').strip()

    if not teacher_name:
        flash('Teacher name cannot be empty.', 'warning')
        return redirect(url_for('main.manage_subjects'))

    existing_teacher = Teacher.query.filter_by(name=teacher_name, admin_id=current_user.id).first()
    if existing_teacher:
        flash(f"Teacher '{teacher_name}' already exists under your profile.", 'warning')
        return redirect(url_for('main.manage_subjects'))

    new_user = None
    if teacher_email and teacher_password:
        if User.query.filter_by(email=teacher_email).first():
            flash(f"Email '{teacher_email}' is already registered to another user.", 'danger')
            return redirect(url_for('main.manage_subjects'))
        
        if len(teacher_password) < 6:
            flash('Teacher password must be at least 6 characters.', 'danger')
            return redirect(url_for('main.manage_subjects'))

        new_user = User(name=teacher_name, email=teacher_email, role='teacher')
        new_user.set_password(teacher_password)
        db.session.add(new_user)
        db.session.flush()

    new_teacher = Teacher(
        name=teacher_name,
        email=teacher_email or None,
        admin_id=current_user.id,
        user_id=new_user.id if new_user else None
    )
    db.session.add(new_teacher)
    db.session.commit()
    
    if new_user:
        flash(f"Teacher '{teacher_name}' and login account '{teacher_email}' created successfully!", 'success')
    else:
        flash(f"Teacher '{teacher_name}' added successfully (no login credentials created).", 'success')

    return redirect(url_for('main.manage_subjects'))

@main_bp.route('/manage_subjects', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_subjects():
    if request.method == 'POST':
        subject_name = request.form.get('subject_name', '').strip()
        subject_code = request.form.get('subject_code', '').strip() or None
        subject_type = request.form.get('subject_type', 'Theory').strip()
        teacher_id = request.form.get('teacher_id')
        class_id = request.form.get('class_id')

        if not all([subject_name, teacher_id, class_id]):
            flash('Subject Name, Class, and Teacher are required.', 'warning')
            return redirect(url_for('main.manage_subjects'))

        existing = Subject.query.filter_by(name=subject_name, class_id=class_id, admin_id=current_user.id).first()
        if existing:
            flash('This subject already exists for this class.', 'warning')
        else:
            new_subject = Subject(
                name=subject_name,
                code=subject_code,
                subject_type=subject_type,
                teacher_id=teacher_id,
                class_id=class_id,
                admin_id=current_user.id
            )
            db.session.add(new_subject)
            db.session.commit()
            flash(f'New {subject_type} subject "{subject_name}" added successfully!', 'success')
        return redirect(url_for('main.manage_subjects'))

    teachers = get_admin_teachers()
    subjects = get_admin_subjects()
    classes = get_admin_classes()
    return render_template('manage_subjects.html', teachers=teachers, subjects=subjects, classes=classes)

@main_bp.route('/delete_teacher/<int:teacher_id>', methods=['POST'])
@login_required
@admin_required
def delete_teacher(teacher_id):
    teacher_to_delete = Teacher.query.get_or_404(teacher_id)
    
    try:
        # 1. Unassign subjects
        for sub in teacher_to_delete.subjects:
            sub.teacher_id = None

        # 2. Clean up proxy transfers where teacher was original OR substitute
        from models import ProxyAttendanceTransfer, TeacherLeave, DailySchedule, Timetable, TeacherEditRequest, TeacherAssignment, AttendanceSession, TeacherDailyAttendance, TeacherAttendanceAuditLog
        ProxyAttendanceTransfer.query.filter(
            (ProxyAttendanceTransfer.substitute_teacher_id == teacher_to_delete.id) |
            (ProxyAttendanceTransfer.original_teacher_id == teacher_to_delete.id)
        ).delete(synchronize_session=False)

        # 3. Clean up leaves where teacher is the applicant OR designated substitute
        TeacherLeave.query.filter(
            (TeacherLeave.teacher_id == teacher_to_delete.id) |
            (TeacherLeave.substitute_teacher_id == teacher_to_delete.id)
        ).delete(synchronize_session=False)

        # 4. Clean up teacher daily attendance and audit logs
        TeacherAttendanceAuditLog.query.filter_by(teacher_id=teacher_to_delete.id).delete(synchronize_session=False)
        TeacherDailyAttendance.query.filter_by(teacher_id=teacher_to_delete.id).delete(synchronize_session=False)

        # 5. Clean up daily schedule proxy allocations
        DailySchedule.query.filter_by(substitute_teacher_id=teacher_to_delete.id).update({'substitute_teacher_id': None, 'is_proxy': False})

        # 6. Nullify timetable slot allocations for this teacher
        Timetable.query.filter_by(teacher_id=teacher_to_delete.id).update({'teacher_id': None})

        # 7. Delete teacher edit requests & assignments
        TeacherEditRequest.query.filter_by(teacher_id=teacher_to_delete.id).delete(synchronize_session=False)
        # 8. Unlink attendance sessions & classes directed as class teacher
        from models import Class
        Class.query.filter_by(class_teacher_id=teacher_to_delete.id).update({'class_teacher_id': None})
        AttendanceSession.query.filter_by(teacher_id=teacher_to_delete.id).update({'teacher_id': None})

        # Find linked user account by user_id or email
        user_account = None
        if teacher_to_delete.user_id:
            user_account = User.query.get(teacher_to_delete.user_id)
        if not user_account and teacher_to_delete.email:
            user_account = User.query.filter_by(email=teacher_to_delete.email.strip().lower()).first()

        email_freed = user_account.email if user_account else (teacher_to_delete.email or teacher_to_delete.name)

        # Free up any linked IssuedTeacherID keys so they immediately show as Vacant
        try:
            issued_keys = IssuedTeacherID.query.filter(
                (IssuedTeacherID.teacher_id == teacher_to_delete.emp_id) |
                (IssuedTeacherID.used_by_user_id == (user_account.id if user_account else -1)) |
                (IssuedTeacherID.email == (teacher_to_delete.email.strip().lower() if teacher_to_delete.email else ''))
            ).all()
            for ik in issued_keys:
                ik.is_used = False
                ik.used_by_user_id = None
                ik.name = None
        except Exception:
            pass

        if user_account:
            db.session.delete(user_account)

        db.session.delete(teacher_to_delete)
        db.session.commit()
        flash(f"Teacher '{teacher_to_delete.name}' ({email_freed}) deleted successfully.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting teacher: {e}", "danger")

    return redirect(url_for('main.enrolled_teachers'))

@main_bp.route('/admin/change_password', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not current_user.check_password(current_password):
            flash('Current master password is incorrect.', 'danger')
            return redirect(url_for('main.dashboard'))

        if len(new_password) < 6:
            flash('New password must be at least 6 characters long.', 'danger')
            return redirect(url_for('main.dashboard'))

        if new_password != confirm_password:
            flash('New passwords do not match.', 'danger')
            return redirect(url_for('main.dashboard'))

        current_user.set_password(new_password)
        db.session.commit()
        flash('Master Admin password updated successfully!', 'success')
        return redirect(url_for('main.dashboard'))

    return redirect(url_for('main.dashboard'))

@main_bp.route('/delete_subject/<int:subject_id>', methods=['POST'])
@login_required
@admin_required
def delete_subject(subject_id):
    subject_to_delete = Subject.query.get_or_404(subject_id)
    if subject_to_delete.admin_id and subject_to_delete.admin_id != current_user.id:
        flash("You do not have permission to delete this subject.", "danger")
        return redirect(url_for('main.manage_subjects'))

    try:
        Attendance.query.filter_by(subject_id=subject_id).delete()
        db.session.delete(subject_to_delete)
        db.session.commit()
        flash(f"Subject '{subject_to_delete.name}' deleted successfully.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting subject: {e}", 'danger')
    return redirect(url_for('main.manage_subjects'))

@main_bp.route('/register_student', methods=['GET', 'POST'])
@login_required
@admin_required
def register_student():
    if request.method == 'POST':
        name = request.form.get('name')
        roll_no = request.form.get('roll_no')
        enrollment_no = request.form.get('enrollment_no')
        department = request.form.get('department', '').strip()
        class_id = request.form.get('class_id')
        student_photo = request.files.get('student_photo')
        captured_base64 = request.form.get('captured_image_base64')

        if not class_id and not department:
            flash('Please select a department and class register.', 'danger')
            return redirect(request.url)
        
        # Check if either base64 captured photo or uploaded photo is provided
        if not (captured_base64 and captured_base64.strip()) and not student_photo:
            flash('No photo was provided.', 'danger')
            return redirect(request.url)

        if Student.query.filter_by(roll_no=roll_no).first() or Student.query.filter_by(enrollment_no=enrollment_no).first():
            flash('A student with this roll or enrollment number already exists.', 'danger')
            return redirect(request.url)

        # Handle face scan
        face_encoding_bytes = None
        filename = None
        
        if captured_base64 and captured_base64.strip():
            result = save_base64_image(captured_base64, roll_no, name, FACES_FOLDER)
            if result:
                filename, filepath = result
                try:
                    image = face_recognition.load_image_file(filepath)
                    encodings = face_recognition.face_encodings(image)
                    if len(encodings) == 1:
                        face_encoding_bytes = encodings[0].tobytes()
                    else:
                        flash(f'{len(encodings)} faces found in captured photo. Please capture a clear image of ONLY one student.', 'danger')
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        return redirect(request.url)
                except Exception as e:
                    flash(f'An error occurred: {e}', 'danger')
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return redirect(request.url)
            else:
                flash('Invalid captured photo.', 'danger')
                return redirect(request.url)
        elif student_photo and student_photo.filename:
            filename = secure_filename(f"{roll_no}_{name}_{student_photo.filename}")
            filepath = os.path.join(FACES_FOLDER, filename)
            student_photo.save(filepath)
            
            try:
                image = face_recognition.load_image_file(filepath)
                encodings = face_recognition.face_encodings(image)
                if len(encodings) == 1:
                    face_encoding_bytes = encodings[0].tobytes()
                else:
                    flash(f'{len(encodings)} faces found. Please use a photo of only one student.', 'danger')
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return redirect(request.url)
            except Exception as e:
                flash(f'An error occurred: {e}', 'danger')
                if os.path.exists(filepath):
                    os.remove(filepath)
                return redirect(request.url)
        else:
            flash('No photo was uploaded.', 'danger')
            return redirect(request.url)

        target_cid = int(class_id) if class_id and class_id.isdigit() else None
        dept_val = department
        if not dept_val and target_cid:
            c = Class.query.get(target_cid)
            if c and c.department:
                dept_val = c.department

        new_student = Student(
            name=name, 
            roll_no=roll_no, 
            enrollment_no=enrollment_no, 
            department=dept_val or 'General',
            class_id=target_cid,
            face_encoding=face_encoding_bytes, 
            image_filename=filename
        )
        db.session.add(new_student)
        db.session.commit()
        flash('Student registered successfully with Department & Class profile!', 'success')
        return redirect(url_for('main.dashboard'))

    classes = get_admin_classes()
    all_departments = Department.query.order_by(Department.name.asc()).all()
    return render_template('register_student.html', classes=classes, all_departments=all_departments)

@main_bp.route('/take_attendance', methods=['GET', 'POST'])
@login_required
@admin_required
def take_attendance():
    today = get_current_date()
    results = None

    if request.method == 'POST':
        class_id = request.form.get('class_id')
        subject_id = request.form.get('subject_id')
        group_photos = request.files.getlist('group_photo')
        captured_base64_list = request.form.getlist('captured_images_base64')

        if not all([class_id, subject_id]):
            flash('Please select both a class and a subject.', 'warning')
            return redirect(url_for('main.take_attendance'))

        has_uploaded_files = any(p.filename for p in group_photos)
        has_captured_photos = any(c.strip() for c in captured_base64_list)

        if not has_uploaded_files and not has_captured_photos:
            flash('No group photos were uploaded or captured.', 'danger')
            return redirect(url_for('main.take_attendance'))

        # Fetch registered students in this class
        known_students = Student.query.filter_by(class_id=class_id).all()
        subject = Subject.query.get(subject_id)
        if not known_students:
            flash('No students are registered in this class.', 'danger')
            return redirect(url_for('main.take_attendance'))

        # Auto-repair missing student face encodings from profile photos
        for s in known_students:
            if s.face_encoding is None and s.image_filename:
                try:
                    photo_path = os.path.join(FACES_FOLDER, s.image_filename)
                    if os.path.exists(photo_path) and face_recognition is not None:
                        img = face_recognition.load_image_file(photo_path)
                        encs = face_recognition.face_encodings(img)
                        if encs:
                            s.face_encoding = encs[0].tobytes()
                            db.session.commit()
                except Exception as repair_err:
                    print(f"Could not auto-generate encoding for student {s.id}: {repair_err}")

        valid_students = [s for s in known_students if s.face_encoding is not None or s.image_filename is not None]
        if not valid_students:
            flash('None of the registered students in this class have uploaded face photos.', 'danger')
            return redirect(url_for('main.take_attendance'))

        known_students_with_encodings = [s for s in valid_students if s.face_encoding is not None and len(s.face_encoding) == 1024]
        known_face_encodings = [np.frombuffer(s.face_encoding, dtype=np.float64) for s in known_students_with_encodings]
        known_student_data = {s.id: s for s in valid_students}
        present_student_ids = set()
        total_faces_found = 0

        # Collect temporary image file paths to process
        temp_photo_paths = []

        # 1. Process uploaded files
        if has_uploaded_files:
            for group_photo in group_photos:
                if group_photo.filename:
                    filename = secure_filename(f"upload_{get_current_datetime_str()}_{group_photo.filename}")
                    filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                    group_photo.save(filepath)
                    temp_photo_paths.append(filepath)

        # 2. Process captured base64 photos
        if has_captured_photos:
            import base64
            import uuid
            for idx, base64_str in enumerate(captured_base64_list):
                if base64_str and (';base64,' in base64_str or base64_str.startswith('data:image/')):
                    try:
                        if ';base64,' in base64_str:
                            format_type, imgstr = base64_str.split(';base64,', 1)
                            ext = format_type.split('/')[-1] if '/' in format_type else 'jpg'
                        else:
                            imgstr = base64_str
                            ext = 'jpg'
                        if ext == 'jpeg':
                            ext = 'jpg'

                        padding = len(imgstr) % 4
                        if padding:
                            imgstr += '=' * (4 - padding)

                        image_data = base64.b64decode(imgstr)
                        filename = f"captured_{get_current_datetime_str()}_{uuid.uuid4().hex[:8]}.{ext}"
                        filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                        with open(filepath, 'wb') as f:
                            f.write(image_data)
                        temp_photo_paths.append(filepath)
                    except Exception as e:
                        flash(f"Error saving captured camera frame {idx + 1}: {e}", "danger")

        # Now run face recognition on all collected photo paths
        for filepath in temp_photo_paths:
            try:
                from face_detector_engine import get_face_biometrics_robust, match_face_encoding
                unknown_image = face_recognition.load_image_file(filepath)
                face_locs, unknown_face_encodings = get_face_biometrics_robust(unknown_image)
                faces_in_photo = len(face_locs)
                total_faces_found += faces_in_photo

                if unknown_face_encodings and known_face_encodings:
                    for face_encoding in unknown_face_encodings:
                        if face_encoding is not None:
                            best_idx, min_dist, is_match, _ = match_face_encoding(face_encoding, known_face_encodings, tolerance=0.58)
                            if is_match and best_idx is not None:
                                student_id = known_students_with_encodings[best_idx].id
                                present_student_ids.add(student_id)

            except Exception as e:
                flash(f"Error processing image {os.path.basename(filepath)}: {e}", "danger")
            finally:
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception as e:
                        print(f"Error removing temp file {filepath}: {e}")



        # Record Present records in AttendanceRecord + AttendanceSession + Attendance
        time_now = get_current_time_str()
        
        sess = AttendanceSession.query.filter_by(subject_id=subject_id, date=today).first()
        if not sess:
            sess = AttendanceSession(
                subject_id=subject_id,
                class_id=subject.class_id,
                teacher_id=subject.teacher_id,
                date=today,
                status='COMPLETED',
                start_time=time_now,
                end_time=time_now
            )
            db.session.add(sess)
            db.session.commit()
        else:
            sess.status = 'COMPLETED'
            db.session.commit()

        for student_id in present_student_ids:
            rec = AttendanceRecord.query.filter_by(session_id=sess.id, student_id=student_id).first()
            if not rec:
                rec = AttendanceRecord(session_id=sess.id, student_id=student_id, status='PRESENT', marked_by='CAMERA')
                db.session.add(rec)
            else:
                rec.status = 'PRESENT'

            if not Attendance.query.filter_by(student_id=student_id, date=today, subject_id=subject_id).first():
                new_attendance = Attendance(
                    student_id=student_id, 
                    date=today, 
                    status='Present', 
                    subject_id=subject_id,
                    time_marked=time_now
                )
                db.session.add(new_attendance)
        db.session.commit()

        # Compile list of present students
        present_students_details = [known_student_data[sid] for sid in present_student_ids if sid in known_student_data]
        results = {
            'total_faces': max(total_faces_found, len(present_students_details)),
            'present_students': present_students_details,
            'subject_name': subject.name
        }
        flash(f"Attendance marked for {len(present_students_details)} student(s) in {subject.name}.", 'success')

    classes = get_admin_classes()
    subjects = get_admin_subjects()
    
    # Restrict today's records view to the admin's students
    admin_student_ids = [s.id for s in get_admin_students()]
    todays_records = AttendanceRecord.query.join(AttendanceSession).filter(
        AttendanceSession.date == today, 
        AttendanceRecord.student_id.in_(admin_student_ids)
    ).all() if admin_student_ids else []
    
    attendance_log = defaultdict(list)
    for record in todays_records:
        sub = record.session.subject if (record.session and record.session.subject) else None
        if sub:
            attendance_log[sub.name].append(record)

    return render_template('take_attendance.html', classes=classes, subjects=subjects,
                           attendance_log=dict(attendance_log), today=today.strftime('%Y-%m-%d'),
                           results=results)

@main_bp.route('/delete_attendance/<int:attendance_id>', methods=['POST'])
@login_required
@admin_required
def delete_attendance(attendance_id):
    record_to_delete = AttendanceRecord.query.get_or_404(attendance_id)
    student = Student.query.get(record_to_delete.student_id)
    admin_classes = [c.id for c in get_admin_classes()]
    if student and student.class_id not in admin_classes:
        flash("Unauthorized action.", "danger")
        return redirect(url_for('main.take_attendance'))

    db.session.delete(record_to_delete)
    db.session.commit()
    flash("Attendance record removed.", "info")
_cv_face_cascade = None
_cv_alt_cascade = None

def get_face_cascades():
    global _cv_face_cascade, _cv_alt_cascade
    try:
        import cv2
        if _cv_face_cascade is None and hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
            _cv_face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            _cv_alt_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_alt2.xml')
    except Exception:
        pass
    return _cv_face_cascade, _cv_alt_cascade

@main_bp.route('/api/live_detect', methods=['POST'])
@login_required
def api_live_detect():
    import base64
    import io
    from PIL import Image

    data = request.get_json() or {}
    image_data = data.get('image', '')
    class_id = data.get('class_id')
    subject_id = data.get('subject_id')

    if not image_data or not image_data.startswith('data:image/'):
        return jsonify({'success': False, 'message': 'No valid image provided'}), 400

    try:
        header, encoded = image_data.split(';base64,')
        padding = len(encoded) % 4
        if padding:
            encoded += '=' * (4 - padding)
        img_bytes = base64.b64decode(encoded)
        
        try:
            pil_img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
            pil_img.load()
        except Exception as img_load_err:
            return jsonify({
                'success': True,
                'total_faces': 0,
                'faces': [],
                'img_width': 640,
                'img_height': 480,
                'message': f'Image decode warning: {img_load_err}'
            })

        # Fast resize for real-time latency optimization (480px gives instant ~30-50ms inference while preserving clear facial landmarks)
        max_dim = 480
        width, height = pil_img.size
        scale = 1.0
        if max(width, height) > max_dim:
            scale = max_dim / float(max(width, height))
            new_w = int(width * scale)
            new_h = int(height * scale)
            pil_img = pil_img.resize((new_w, new_h), Image.Resampling.BILINEAR)

        img_np = np.array(pil_img)

        # Detect face bounding boxes & 128-d embeddings using low-latency live mode
        from face_detector_engine import get_face_biometrics_robust, match_face_encoding
        face_locations, face_encodings = get_face_biometrics_robust(img_np, enable_cnn=False, fast_live_mode=True)

        if not face_locations:
            return jsonify({
                'success': True,
                'total_faces': 0,
                'faces': [],
                'img_width': width,
                'img_height': height
            })

        # Query valid students directly from DB without disk I/O on live loop
        target_students = []
        if class_id and str(class_id).isdigit():
            target_students = Student.query.filter(
                Student.class_id == int(class_id),
                Student.face_encoding.isnot(None)
            ).all()
        elif subject_id and str(subject_id).isdigit():
            sub = Subject.query.get(int(subject_id))
            if sub and sub.class_id:
                target_students = Student.query.filter(
                    Student.class_id == sub.class_id,
                    Student.face_encoding.isnot(None)
                ).all()

        if not target_students:
            target_students = Student.query.filter(Student.face_encoding.isnot(None)).all()

        valid_students = [s for s in target_students if s.face_encoding and len(s.face_encoding) == 1024]
        known_encodings = [np.frombuffer(s.face_encoding, dtype=np.float64) for s in valid_students]

        # Additional fallback pool: all registered students across classes
        all_students = []
        all_student_encodings = []
        if (class_id or subject_id) and len(valid_students) < 50:
            all_students = [s for s in Student.query.filter(Student.face_encoding.isnot(None)).all() if len(s.face_encoding) == 1024]
            all_student_encodings = [np.frombuffer(s.face_encoding, dtype=np.float64) for s in all_students]

        # Faculty pool: ensure teachers testing the camera are accurately recognized
        teachers = [t for t in Teacher.query.filter(Teacher.face_encoding.isnot(None)).all() if len(t.face_encoding) == 1024]
        teacher_encodings = [np.frombuffer(t.face_encoding, dtype=np.float64) for t in teachers]

        detected_faces = []
        for loc, encoding in zip(face_locations, face_encodings):
            top, right, bottom, left = loc
            if scale != 1.0:
                top = int(top / scale)
                right = int(right / scale)
                bottom = int(bottom / scale)
                left = int(left / scale)

            matched_name = "Unknown Face"
            matched_roll = ""
            match_found = False
            confidence_str = "0%"

            # 1. Check target students pool
            if encoding is not None and known_encodings:
                best_idx, min_dist, is_match, conf_str = match_face_encoding(encoding, known_encodings, tolerance=0.60)
                if is_match and best_idx is not None:
                    matched_student = valid_students[best_idx]
                    matched_name = matched_student.name
                    matched_roll = matched_student.roll_no or ""
                    match_found = True
                    confidence_str = conf_str

            # 2. Check full student pool fallback if not found in filtered class
            if not match_found and encoding is not None and all_student_encodings:
                best_idx, min_dist, is_match, conf_str = match_face_encoding(encoding, all_student_encodings, tolerance=0.60)
                if is_match and best_idx is not None:
                    matched_student = all_students[best_idx]
                    matched_name = matched_student.name
                    matched_roll = matched_student.roll_no or ""
                    match_found = True
                    confidence_str = conf_str

            # 3. Check faculty pool (so teachers are identified during setup/testing)
            if not match_found and encoding is not None and teacher_encodings:
                best_idx, min_dist, is_match, conf_str = match_face_encoding(encoding, teacher_encodings, tolerance=0.60)
                if is_match and best_idx is not None:
                    matched_teacher = teachers[best_idx]
                    matched_name = matched_teacher.name
                    matched_roll = "Faculty"
                    match_found = True
                    confidence_str = conf_str

            detected_faces.append({
                'box': {'top': top, 'right': right, 'bottom': bottom, 'left': left},
                'name': matched_name,
                'roll_no': matched_roll,
                'matched': match_found,
                'confidence': confidence_str
            })

        return jsonify({
            'success': True,
            'total_faces': len(detected_faces),
            'faces': detected_faces,
            'img_width': width,
            'img_height': height
        })

    except Exception as e:
        print(f"Error in api_live_detect: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@main_bp.route('/delete_todays_attendance', methods=['POST'])
@login_required
@admin_required
def delete_todays_attendance():
    today = get_current_date()
    admin_student_ids = [s.id for s in get_admin_students()]
    try:
        # Delete only records belonging to the admin's students
        Attendance.query.filter(
            Attendance.date == today, 
            Attendance.student_id.in_(admin_student_ids)
        ).delete(synchronize_session=False)
        db.session.commit()
        flash("All of today's attendance records for your students have been deleted.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"An error occurred: {e}", 'danger')
    return redirect(url_for('main.take_attendance'))

@main_bp.route('/reports')
@login_required
@admin_required
def view_reports():
    classes = get_admin_classes()
    all_departments = Department.query.order_by(Department.name.asc()).all()
    dept_filter = request.args.get('department', '').strip()
    class_id = request.args.get('class_id', '').strip()
    
    if class_id == 'unclassified':
        students = Student.query.filter(Student.class_id == None).all()
        selected_class_id = 'unclassified'
    elif class_id and class_id.isdigit():
        class_id_int = int(class_id)
        if class_id_int in [c.id for c in classes]:
            students = Student.query.filter_by(class_id=class_id_int).all()
            selected_class_id = class_id_int
        else:
            students = []
            selected_class_id = None
    else:
        students = get_admin_students()
        selected_class_id = None

    if dept_filter:
        students = [s for s in students if (s.department and s.department.upper() == dept_filter.upper()) or (s.class_assigned and s.class_assigned.department and s.class_assigned.department.upper() == dept_filter.upper())]

    # Sort by class name and student name
    students = sorted(students, key=lambda x: (x.class_assigned.name if x.class_assigned else '', x.name))
    return render_template(
        'view_reports.html', 
        students=students, 
        classes=classes, 
        all_departments=all_departments,
        selected_dept=dept_filter,
        selected_class_id=selected_class_id
    )

import random
import string

def generate_unique_teacher_id():
    """Generates a unique teacher ID string (e.g. TCH-849201)."""
    while True:
        code = 'TCH-' + ''.join(random.choices(string.digits, k=6))
        if not IssuedTeacherID.query.filter_by(teacher_id=code).first():
            return code

@main_bp.route('/enrolled_teachers')
@login_required
@admin_required
def enrolled_teachers():
    class_id = request.args.get('class_id')
    subject_id = request.args.get('subject_id')

    classes = get_admin_classes()
    subjects = Subject.query.all()

    query = Teacher.query.filter(Teacher.status != 'Pending')

    if class_id and class_id != 'all':
        try:
            cid = int(class_id)
            query = query.filter(
                (Teacher.id.in_(db.session.query(Class.class_teacher_id).filter(Class.id == cid))) |
                (Teacher.id.in_(db.session.query(Subject.teacher_id).filter(Subject.class_id == cid)))
            )
        except ValueError:
            pass

    if subject_id and subject_id != 'all':
        try:
            sid = int(subject_id)
            query = query.filter(Teacher.subjects.any(Subject.id == sid))
        except ValueError:
            pass

    teachers = query.order_by(Teacher.name).all()

    # Auto-sync teacher department from assigned class/subjects if currently 'General'
    for t in teachers:
        if (not t.department or t.department.strip().lower() == 'general') and t.effective_department != 'General':
            t.department = t.effective_department
    db.session.commit()

    # Naturally sort Issued Teacher IDs and enrich with linked teacher details
    raw_issued = IssuedTeacherID.query.all()
    for item in raw_issued:
        t_rec = None
        if item.teacher_id:
            t_rec = Teacher.query.filter_by(emp_id=item.teacher_id).first()
        if not t_rec and item.used_by_user_id:
            t_rec = Teacher.query.filter_by(user_id=item.used_by_user_id).first()
        if not t_rec and item.email:
            t_rec = Teacher.query.filter_by(email=item.email.strip().lower()).first()

        if t_rec:
            item.is_used = True
            item.allotted_email = t_rec.email
            item.allotted_name = t_rec.name
        else:
            # If no teacher matches, mark as vacant / unused
            if item.is_used and not item.email:
                item.is_used = False
                item.used_by_user_id = None
            item.allotted_email = item.email
            item.allotted_name = item.name

    def natural_key_sort(item):
        val = str(item.teacher_id or '').strip()
        digits = ''.join(c for c in val if c.isdigit())
        if digits and val.isdigit():
            return (0, int(digits), val)
        elif digits:
            return (1, int(digits), val)
        return (2, 0, val)

    issued_teacher_ids = sorted(raw_issued, key=natural_key_sort)
    all_subject_names = [s[0] for s in db.session.query(Subject.name).distinct().order_by(Subject.name).all()]

    return render_template(
        'enrolled_teachers.html',
        teachers=teachers,
        classes=classes,
        subjects=subjects,
        issued_teacher_ids=issued_teacher_ids,
        all_subject_names=all_subject_names,
        selected_class_id=int(class_id) if class_id and class_id.isdigit() else None,
        selected_subject_id=int(subject_id) if subject_id and subject_id.isdigit() else None
    )

@main_bp.route('/admin/assign_teacher_workload/<int:teacher_id>', methods=['POST'])
@login_required
@admin_required
def assign_teacher_workload(teacher_id):
    teacher = Teacher.query.get_or_404(teacher_id)
    
    # 0. Update Faculty Department
    department = request.form.get('department', '').strip()
    if department:
        teacher.department = department

    # 1. Update Class Teacher Leadership (multiple classes)
    assigned_class_ids = request.form.getlist('class_teacher_for')
    assigned_class_ids_int = [int(cid) for cid in assigned_class_ids if cid.isdigit()]

    # Clear previous class teacher assignments for this teacher
    previous_classes = Class.query.filter_by(class_teacher_id=teacher.id).all()
    for c in previous_classes:
        if c.id not in assigned_class_ids_int:
            c.class_teacher_id = None
            
    # Set new class teacher assignments
    for cid in assigned_class_ids_int:
        c = Class.query.get(cid)
        if c:
            c.class_teacher_id = teacher.id

    # 2. Update Subject Teacher assignments (multiple subjects across classes)
    assigned_subject_ids = request.form.getlist('subject_ids')
    assigned_subject_ids_int = [int(sid) for sid in assigned_subject_ids if sid.isdigit()]

    admin_subjects = get_admin_subjects()
    for sub in admin_subjects:
        if sub.id in assigned_subject_ids_int:
            sub.teacher_id = teacher.id

    # 3. Handle quick new subject creation & direct assignment if supplied
    new_sub_name = request.form.get('new_subject_name', '').strip()
    new_sub_class_id = request.form.get('new_subject_class_id')
    if new_sub_name and new_sub_class_id and new_sub_class_id.isdigit():
        target_cid = int(new_sub_class_id)
        new_sub = Subject(
            name=new_sub_name,
            class_id=target_cid,
            teacher_id=teacher.id,
            admin_id=current_user.id
        )
        db.session.add(new_sub)

    # Auto-resolve department if not explicitly set
    if (not teacher.department or teacher.department.strip().lower() == 'general') and teacher.effective_department != 'General':
        teacher.department = teacher.effective_department

    try:
        db.session.commit()
        flash(f"Workload assignments for '{teacher.name}' updated successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating workload assignment: {e}", "danger")

    return redirect(url_for('main.enrolled_teachers'))

@main_bp.route('/admin/generate_teacher_id', methods=['POST'])
@login_required
@admin_required
def generate_teacher_id():
    custom_id = request.form.get('custom_id', '').strip()
    target_email = request.form.get('target_email', '').strip()
    target_name = request.form.get('target_name', '').strip()

    if custom_id:
        teacher_id_code = custom_id.upper()
        if IssuedTeacherID.query.filter_by(teacher_id=teacher_id_code).first():
            flash(f"Teacher ID '{teacher_id_code}' already exists.", "danger")
            return redirect(url_for('main.enrolled_teachers'))
        if Teacher.query.filter_by(emp_id=teacher_id_code).first():
            flash(f"A teacher with ID '{teacher_id_code}' is already registered.", "danger")
            return redirect(url_for('main.enrolled_teachers'))
    else:
        teacher_id_code = generate_unique_teacher_id()

    try:
        new_issued_id = IssuedTeacherID(
            teacher_id=teacher_id_code,
            email=target_email or None,
            name=target_name or None,
            created_by_admin_id=current_user.id
        )
        db.session.add(new_issued_id)
        db.session.commit()
        flash(f"Teacher ID generated successfully: '{teacher_id_code}'. Share this code with the teacher for registration.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error issuing Teacher ID: {e}", "danger")

    return redirect(url_for('main.enrolled_teachers'))

@main_bp.route('/admin/revoke_teacher_id/<int:id_id>', methods=['POST'])
@login_required
@admin_required
def revoke_teacher_id(id_id):
    issued_item = IssuedTeacherID.query.get_or_404(id_id)
    try:
        code = issued_item.teacher_id
        db.session.delete(issued_item)
        db.session.commit()
        flash(f"Issued Teacher Code '{code}' has been revoked/deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error revoking Teacher Code: {e}", "danger")

    return redirect(url_for('main.enrolled_teachers'))

@main_bp.route('/delete_student/<int:student_id>', methods=['POST'])
@login_required
@admin_required
def delete_student(student_id):
    student_to_delete = Student.query.get_or_404(student_id)
    referrer = request.referrer
    try:
        if student_to_delete.image_filename:
            image_filepath = os.path.join(FACES_FOLDER, student_to_delete.image_filename)
            if os.path.exists(image_filepath): 
                os.remove(image_filepath)
                
        # Find and delete ALL matching user login accounts by user_id or email
        user_accounts_to_delete = set()
        if student_to_delete.user_id:
            u1 = User.query.get(student_to_delete.user_id)
            if u1:
                user_accounts_to_delete.add(u1)
        if hasattr(student_to_delete, 'user') and student_to_delete.user:
            user_accounts_to_delete.add(student_to_delete.user)

        email_freed = ""
        for u in user_accounts_to_delete:
            email_freed = u.email
            session.pop('reg_otp_' + u.email.lower(), None) if u.email else None
            db.session.delete(u)

        # Delete all linked attendance audit logs, discrepancy requests, attendance records, and edit requests
        from models import AttendanceAuditLog, AttendanceRecord, AttendanceDiscrepancyRequest
        AttendanceAuditLog.query.filter_by(student_id=student_to_delete.id).delete()
        AttendanceDiscrepancyRequest.query.filter_by(student_id=student_to_delete.id).delete()
        AttendanceRecord.query.filter_by(student_id=student_to_delete.id).delete()
        Attendance.query.filter_by(student_id=student_to_delete.id).delete()
        StudentEditRequest.query.filter_by(student_id=student_to_delete.id).delete()

        db.session.delete(student_to_delete)
        db.session.commit()
        flash(f'Student "{student_to_delete.name}" and login account ({email_freed}) deleted successfully! Email is now free for re-registration.', 'success')
    except Exception as e:
        print(f"[ERROR IN DELETE STUDENT] {e}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        flash(f'Error deleting student: {e}', 'danger')

    if referrer and 'dashboard' in referrer:
        return redirect(url_for('main.dashboard'))
    else:
        return redirect(url_for('main.view_reports'))

@main_bp.route('/uploads/faces/<path:filename>')
def uploaded_face(filename):
    faces_dir1 = os.path.join(os.getcwd(), 'uploads', 'faces')
    faces_dir2 = os.path.join(os.getcwd(), 'temp_uploads', 'faces')
    os.makedirs(faces_dir1, exist_ok=True)
    os.makedirs(faces_dir2, exist_ok=True)

    # 1. If file already exists on local container disk, return it directly
    target2 = os.path.join(faces_dir2, filename)
    target1 = os.path.join(faces_dir1, filename)
    if os.path.exists(target2) and os.path.getsize(target2) > 0:
        return send_from_directory(faces_dir2, filename)
    if os.path.exists(target1) and os.path.getsize(target1) > 0:
        return send_from_directory(faces_dir1, filename)

    # 2. Self-healing from Persistent Neon PostgreSQL Database Base64 Storage
    try:
        # Check Teacher
        teacher = Teacher.query.filter_by(image_filename=filename).first()
        if teacher and teacher.image_data:
            data = teacher.image_data
            if ',' in data:
                data = data.split(',', 1)[1]
            img_bytes = base64.b64decode(data)
            with open(target2, 'wb') as f:
                f.write(img_bytes)
            mimetype = 'image/png' if filename.lower().endswith('.png') else 'image/jpeg'
            return send_file(io.BytesIO(img_bytes), mimetype=mimetype)

        # Check Student
        student = Student.query.filter_by(image_filename=filename).first()
        if student and student.image_data:
            data = student.image_data
            if ',' in data:
                data = data.split(',', 1)[1]
            img_bytes = base64.b64decode(data)
            with open(target2, 'wb') as f:
                f.write(img_bytes)
            mimetype = 'image/png' if filename.lower().endswith('.png') else 'image/jpeg'
            return send_file(io.BytesIO(img_bytes), mimetype=mimetype)

        # Check StudentEditRequest
        req_item = StudentEditRequest.query.filter_by(new_image_filename=filename).first()
        if req_item and req_item.new_image_data:
            data = req_item.new_image_data
            if ',' in data:
                data = data.split(',', 1)[1]
            img_bytes = base64.b64decode(data)
            with open(target2, 'wb') as f:
                f.write(img_bytes)
            mimetype = 'image/png' if filename.lower().endswith('.png') else 'image/jpeg'
            return send_file(io.BytesIO(img_bytes), mimetype=mimetype)
    except Exception as e:
        print(f"[Uploaded Face Recovery Error] Could not restore {filename} from Neon DB: {e}")

    # 3. Graceful SVG default avatar fallback (prevents broken ? icon)
    name_seed = filename.replace('_', ' ').replace('-', ' ').split('.')[0][:15]
    return redirect(f"https://ui-avatars.com/api/?name={name_seed}&background=6366f1&color=fff&size=128&bold=true")

@main_bp.route('/uploads/university/<path:filename>')
def uploaded_university_logo(filename):
    u_dir = os.path.join(os.getcwd(), 'uploads', 'university')
    os.makedirs(u_dir, exist_ok=True)
    target_path = os.path.join(u_dir, filename)

    # 1. If file already exists on local container disk, return it directly
    if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        return send_from_directory(u_dir, filename)

    # 2. Self-healing from Persistent Neon PostgreSQL Database Base64 Storage
    try:
        settings = UniversitySettings.get_settings()
        
        # Check if requested filename is the logo
        if settings.logo_filename == filename and settings.logo_data:
            data = settings.logo_data
            if ',' in data:
                data = data.split(',', 1)[1]
            img_bytes = base64.b64decode(data)
            with open(target_path, 'wb') as f:
                f.write(img_bytes)
            mimetype = 'image/png' if filename.endswith('.png') else 'image/jpeg'
            return send_file(io.BytesIO(img_bytes), mimetype=mimetype)

        # Check if requested filename is the name graphic
        if settings.name_image_filename == filename and settings.name_image_data:
            data = settings.name_image_data
            if ',' in data:
                data = data.split(',', 1)[1]
            img_bytes = base64.b64decode(data)
            with open(target_path, 'wb') as f:
                f.write(img_bytes)
            mimetype = 'image/png' if filename.endswith('.png') else 'image/jpeg'
            return send_file(io.BytesIO(img_bytes), mimetype=mimetype)

        # Check if requested filename is the signature
        if settings.signature_filename == filename and settings.signature_data:
            data = settings.signature_data
            if ',' in data:
                data = data.split(',', 1)[1]
            img_bytes = base64.b64decode(data)
            with open(target_path, 'wb') as f:
                f.write(img_bytes)
            mimetype = 'image/png' if filename.endswith('.png') else 'image/jpeg'
            return send_file(io.BytesIO(img_bytes), mimetype=mimetype)
    except Exception as e:
        print(f"[Uploads Recovery Error] Could not restore {filename} from Neon DB: {e}")

    # 3. Fallback to bundled static repository assets
    static_univ_dir = os.path.join(os.getcwd(), 'static', 'images', 'university')
    static_asset = os.path.join(static_univ_dir, filename)
    if os.path.exists(static_asset):
        return send_from_directory(static_univ_dir, filename)
    
    # 4. Graceful default fallbacks based on file prefix
    if 'logo' in filename.lower():
        fallback_logo = os.path.join(static_univ_dir, 'parul_logo.png')
        if os.path.exists(fallback_logo):
            return send_from_directory(static_univ_dir, 'parul_logo.png')
        return send_from_directory(os.path.join(os.getcwd(), 'static', 'images'), 'logo.png')
    elif 'name' in filename.lower():
        fallback_name = os.path.join(static_univ_dir, 'parul_name.png')
        if os.path.exists(fallback_name):
            return send_from_directory(static_univ_dir, 'parul_name.png')
    elif 'signature' in filename.lower():
        fallback_sig = os.path.join(static_univ_dir, 'parul_signature.png')
        if os.path.exists(fallback_sig):
            return send_from_directory(static_univ_dir, 'parul_signature.png')

    return send_from_directory(os.path.join(os.getcwd(), 'static', 'images'), 'logo.png')

@main_bp.route('/get_subjects/<int:class_id>')
@login_required
def get_subjects(class_id):
    # Expose subjects of a class, ensuring the class is visible
    class_item = Class.query.get_or_404(class_id)
    if class_item.admin_id and class_item.admin_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
        
    subjects = Subject.query.filter_by(class_id=class_id).all()
    subject_list = [
        {
            'id': subject.id,
            'name': subject.name,
            'teacher_name': subject.teacher.name if subject.teacher else 'No Teacher'
        } for subject in subjects
    ]
    return jsonify({'subjects': subject_list})

@main_bp.route('/admin/approvals')
@login_required
@admin_required
def approvals():
    admin_classes = [c.id for c in get_admin_classes()]
    edit_requests = StudentEditRequest.query.join(Student).filter(
        Student.class_id.in_(admin_classes),
        StudentEditRequest.status == 'Pending'
    ).order_by(StudentEditRequest.created_at.desc()).all()

    teacher_edit_requests = TeacherEditRequest.query.join(Teacher).filter(
        TeacherEditRequest.status == 'Pending'
    ).order_by(TeacherEditRequest.created_at.desc()).all()
    
    pending_teachers = Teacher.query.filter(Teacher.status.in_(['Pending', 'Email_Verified'])).order_by(Teacher.id.desc()).all()
    all_teachers = get_admin_teachers()
    
    pending_leaves = TeacherLeave.query.filter_by(status='PENDING').order_by(TeacherLeave.id.desc()).all()
    all_leaves = TeacherLeave.query.order_by(TeacherLeave.id.desc()).limit(100).all()
    pending_corrections = CorrectionRequest.query.filter_by(status='PENDING').order_by(CorrectionRequest.id.desc()).all()
    emergency_desk = compute_emergency_proxy_desk(get_current_date())

    return render_template(
        'admin_approvals.html',
        requests=edit_requests,
        teacher_edit_requests=teacher_edit_requests,
        pending_teachers=pending_teachers,
        all_teachers=all_teachers,
        pending_leaves=pending_leaves,
        all_leaves=all_leaves,
        pending_corrections=pending_corrections,
        emergency_desk=emergency_desk
    )

@main_bp.route('/admin/emergency_proxy_desk')
@login_required
@admin_required
def emergency_proxy_desk():
    today = get_current_date()
    
    # Date selected for active desk
    date_param = request.args.get('date')
    selected_date = today
    if date_param:
        try:
            selected_date = datetime.strptime(date_param, '%Y-%m-%d').date()
        except Exception:
            selected_date = today

    emergency_desk_data = compute_emergency_proxy_desk(selected_date)
    
    # History filters & active tab
    active_tab = request.args.get('tab', '').strip()
    history_date_str = request.args.get('history_date', '').strip()
    history_status = request.args.get('history_status', 'ALL').strip()
    history_class_id = request.args.get('history_class_id', type=int)

    if history_date_str or (history_status and history_status != 'ALL') or history_class_id or active_tab == 'history':
        active_tab = 'history'
    else:
        active_tab = 'today'

    history_query = DailySchedule.query.join(Timetable).filter(
        (DailySchedule.is_proxy == True) | (DailySchedule.is_cancelled == True)
    )

    if history_date_str:
        try:
            h_date = datetime.strptime(history_date_str, '%Y-%m-%d').date()
            history_query = history_query.filter(DailySchedule.date == h_date)
        except Exception:
            pass

    if history_status == 'PROXY':
        history_query = history_query.filter(DailySchedule.is_proxy == True, DailySchedule.is_cancelled == False)
    elif history_status == 'CANCELLED':
        history_query = history_query.filter(DailySchedule.is_cancelled == True)

    if history_class_id:
        history_query = history_query.filter(Timetable.class_id == history_class_id)

    proxy_history = history_query.order_by(DailySchedule.date.desc(), DailySchedule.id.desc()).limit(200).all()
    all_classes = get_admin_classes()

    return render_template(
        'admin_emergency_proxy_desk.html',
        emergency_desk=emergency_desk_data,
        proxy_history=proxy_history,
        today=today,
        selected_date=selected_date,
        history_date_str=history_date_str,
        history_status=history_status,
        history_class_id=history_class_id,
        active_tab=active_tab,
        all_classes=all_classes
    )

@main_bp.route('/admin/teacher_approval/<int:teacher_id>/<action>', methods=['POST'])
@login_required
@admin_required
def handle_teacher_approval(teacher_id, action):
    teacher = Teacher.query.get_or_404(teacher_id)
    user_acc = User.query.get(teacher.user_id) if teacher.user_id else None

    if action == 'approve':
        try:
            teacher.status = 'Approved'
            if user_acc:
                user_acc.status = 'Approved'
            db.session.commit()
            flash(f"Teacher account for '{teacher.name}' approved successfully! They can now log in.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error approving teacher: {e}", "danger")
    elif action == 'reject':
        try:
            # 1. Unassign subjects assigned to this teacher
            for sub in teacher.subjects:
                sub.teacher_id = None

            # 2. Release & reset any linked IssuedTeacherID keys so they immediately show as Vacant
            try:
                issued_keys = IssuedTeacherID.query.filter(
                    (IssuedTeacherID.teacher_id == teacher.emp_id) |
                    (IssuedTeacherID.used_by_user_id == (user_acc.id if user_acc else -1)) |
                    (IssuedTeacherID.email == (teacher.email.strip().lower() if teacher.email else ''))
                ).all()
                for ik in issued_keys:
                    ik.is_used = False
                    ik.used_by_user_id = None
                    ik.name = None
            except Exception:
                pass

            # 3. Clean up teacher preferences / auxiliary records
            try:
                TeacherSubjectChoice.query.filter_by(teacher_id=teacher.id).delete()
            except Exception:
                pass
            try:
                TeacherLeave.query.filter_by(teacher_id=teacher.id).delete()
            except Exception:
                pass
            try:
                TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id).delete()
            except Exception:
                pass
            try:
                TeacherWeeklyTimetable.query.filter_by(teacher_id=teacher.id).delete()
            except Exception:
                pass
            try:
                TeacherDismissedNotice.query.filter_by(teacher_id=teacher.id).delete()
            except Exception:
                pass
            try:
                TeacherReadNotice.query.filter_by(teacher_id=teacher.id).delete()
            except Exception:
                pass

            # 4. Remove face photo from disk if present
            if teacher.image_filename:
                photo_path = os.path.join(FACES_FOLDER, teacher.image_filename)
                if os.path.exists(photo_path):
                    try:
                        os.remove(photo_path)
                    except Exception:
                        pass

            # 5. Delete Teacher entity and linked User account safely
            db.session.delete(teacher)
            if user_acc:
                db.session.delete(user_acc)

            db.session.commit()
            flash(f"Teacher registration for '{teacher.name}' rejected and removed. Issued Teacher ID is now free for re-use.", "info")
        except Exception as e:
            db.session.rollback()
            flash(f"Error rejecting teacher: {e}", "danger")

    return redirect(url_for('main.approvals'))

@main_bp.route('/admin/approval/<int:request_id>/<action>', methods=['POST'])
@login_required
@admin_required
def handle_approval(request_id, action):
    req = StudentEditRequest.query.get_or_404(request_id)
    
    # Check permissions
    admin_classes = [c.id for c in get_admin_classes()]
    if req.student.class_id not in admin_classes:
        flash("You do not have permission to review this request.", "danger")
        return redirect(url_for('main.approvals'))

    student = req.student
    
    if action == 'approve':
        try:
            # Delete old face photo from disk if new photo was uploaded
            if req.new_image_filename and req.new_image_filename != student.image_filename:
                # Rename the new image from pending_ to clean name
                old_path = os.path.join(FACES_FOLDER, student.image_filename) if student.image_filename else None
                if old_path and os.path.exists(old_path):
                    os.remove(old_path)

            # Update student record
            student.name = req.new_name
            student.roll_no = req.new_roll_no
            student.enrollment_no = req.new_enrollment_no
            student.class_id = req.new_class_id
            if req.new_department:
                student.department = req.new_department
            elif req.new_class_id:
                c = Class.query.get(req.new_class_id)
                if c and c.department:
                    student.department = c.department
            
            if hasattr(req, 'new_mobile') and req.new_mobile:
                student.mobile = req.new_mobile
            if hasattr(req, 'new_parent_name') and req.new_parent_name:
                student.parent_name = req.new_parent_name
            if hasattr(req, 'new_parent_email') and req.new_parent_email:
                student.parent_email = req.new_parent_email
            if hasattr(req, 'new_parent_mobile') and req.new_parent_mobile:
                student.parent_mobile = req.new_parent_mobile

            if req.new_image_filename:
                student.image_filename = req.new_image_filename
            if req.new_face_encoding:
                student.face_encoding = req.new_face_encoding
                student.face_embedding = req.new_face_encoding
            if req.new_image_data:
                student.image_data = req.new_image_data
            
            # If student has a linked User account, update their display name and mobile too
            if student.user_id:
                user = User.query.get(student.user_id)
                if user:
                    user.name = req.new_name
                    if hasattr(req, 'new_mobile') and req.new_mobile:
                        user.mobile = req.new_mobile

            db.session.delete(req) # Remove request from queue
            db.session.commit()
            flash(f"Profile change request for student '{student.name}' approved successfully! All changes are now live.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"An error occurred while approving: {e}", "danger")
            
    elif action == 'reject':
        try:
            # If they uploaded a new face photo, delete that pending photo from disk to save space
            if req.new_image_filename and req.new_image_filename != student.image_filename:
                filepath = os.path.join(FACES_FOLDER, req.new_image_filename)
                if os.path.exists(filepath):
                    os.remove(filepath)
            
            db.session.delete(req) # Remove request from queue
            db.session.commit()
            flash(f"Profile change request for student '{student.name}' rejected and discarded.", "info")
        except Exception as e:
            db.session.rollback()
            flash(f"An error occurred: {e}", "danger")

    return redirect(url_for('main.approvals'))

@main_bp.route('/admin/teacher_edit_approval/<int:request_id>/<action>', methods=['POST'])
@login_required
@admin_required
def handle_teacher_edit_approval(request_id, action):
    req = TeacherEditRequest.query.get_or_404(request_id)
    teacher = req.teacher

    if action == 'approve':
        try:
            # Delete old face photo from disk if new photo was uploaded
            if req.new_image_filename and req.new_image_filename != teacher.image_filename:
                old_path = os.path.join(FACES_FOLDER, teacher.image_filename) if teacher.image_filename else None
                if old_path and os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass

            # Update teacher entity fields
            teacher.name = req.new_name
            if req.new_emp_id:
                teacher.emp_id = req.new_emp_id
                teacher.employee_code = req.new_emp_id
            if req.new_department:
                teacher.department = req.new_department
            teacher.mobile = req.new_mobile
            teacher.primary_subject = req.new_primary_subject
            teacher.secondary_subject = req.new_secondary_subject
            teacher.tertiary_subject = req.new_tertiary_subject

            if req.new_image_filename:
                teacher.image_filename = req.new_image_filename
                teacher.image_data = req.new_image_data
                teacher.face_encoding = req.new_face_encoding

            # Update linked User account
            if teacher.user_id:
                u_acc = User.query.get(teacher.user_id)
                if u_acc:
                    u_acc.name = req.new_name
                    if req.new_mobile:
                        u_acc.mobile = req.new_mobile

            db.session.delete(req) # Remove request from queue upon approval
            db.session.commit()
            flash(f"Profile change request for faculty member '{teacher.name}' approved and applied live successfully.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error approving teacher profile changes: {e}", "danger")
    elif action == 'reject':
        try:
            if req.new_image_filename and req.new_image_filename != teacher.image_filename:
                photo_path = os.path.join(FACES_FOLDER, req.new_image_filename)
                if os.path.exists(photo_path):
                    try:
                        os.remove(photo_path)
                    except Exception:
                        pass
            db.session.delete(req) # Remove request from queue
            db.session.commit()
            flash(f"Profile change request for faculty member '{teacher.name}' rejected.", "info")
        except Exception as e:
            db.session.rollback()
            flash(f"Error rejecting teacher profile changes: {e}", "danger")

    return redirect(url_for('main.approvals'))

@main_bp.route('/retention_risk_report')
@login_required
@admin_required
def retention_risk_report():
    """Generates and displays the detailed report on at-risk students."""
    from datetime import date, timedelta
    
    risk_students = get_retention_risk_students()

    today = get_current_date()
    five_days_ago = today - timedelta(days=5)

    student_details = []
    for student in risk_students:
        stats = calculate_student_attendance(student.id)
        pct = stats['percentage']

        c_teacher = student.class_assigned.class_teacher if student.class_assigned else None

        student_details.append({
            'student': student,
            'class_name': student.class_assigned.name if student.class_assigned else 'No Class',
            'class_teacher_name': c_teacher.name if c_teacher else 'Unassigned',
            'class_teacher_id': c_teacher.id if c_teacher else None,
            'attendance_pct': pct,
            'parent_phone': student.mobile or 'N/A',
            'parent_email': student.user_account.email if student.user_account else 'N/A'
        })

    return render_template(
        'retention_risk_report.html',
        risk_students_data=student_details,
        report_start_date=five_days_ago.strftime('%Y-%m-%d'),
        report_end_date=today.strftime('%Y-%m-%d'),
        total_risk_students=len(student_details)
    )

@main_bp.route('/admin/forward_retention_list', methods=['POST'])
@login_required
@admin_required
def forward_retention_list():
    from models import ClassAnnouncement
    risk_students = get_retention_risk_students()
    if not risk_students:
        flash("No active retention risk students to forward.", "info")
        return redirect(url_for('main.retention_risk_report'))

    # Group students by class
    class_groups = {}
    for st in risk_students:
        if st.class_id:
            class_groups.setdefault(st.class_id, []).append(st)

    forwarded_count = 0
    for class_id, students in class_groups.items():
        cls = Class.query.get(class_id)
        if cls and cls.class_teacher_id:
            student_names = ", ".join([f"{s.name} ({s.roll_no})" for s in students])
            ann = ClassAnnouncement(
                class_id=cls.id,
                teacher_id=cls.class_teacher_id,
                title="URGENT: Retention Risk Forwarded by Admin",
                content=f"Administrator has forwarded the active 5-day retention risk list for {cls.name}:\n\nStudents At Risk: {student_names}\n\nPlease initiate parent contact immediately.",
                notice_type="RetentionAlert"
            )
            db.session.add(ann)
            forwarded_count += 1

    if forwarded_count > 0:
        db.session.commit()
        flash(f"Successfully forwarded retention risk notices to {forwarded_count} Class Teacher(s)!", "success")
    else:
        flash("No assigned Class Teachers found for the flagged risk students.", "warning")

    return redirect(url_for('main.retention_risk_report'))

@main_bp.route('/download_risk_report')
@login_required
@admin_required
def download_risk_report():
    """Generates a CSV file of the retention risk students for download."""
    import io
    import csv
    from flask import make_response
    
    risk_students = get_retention_risk_students()
    today = date.today()

    si = io.StringIO()
    cw = csv.writer(si)

    # CSV Header
    cw.writerow(['Name', 'Roll Number', 'Enrollment Number', 'Class Name', 'Status (Last 5 Days)'])

    # CSV Data Rows
    for student in risk_students:
        cw.writerow([
            student.name,
            student.roll_no,
            student.enrollment_no,
            student.class_assigned.name if student.class_assigned else 'No Class',
            'ZERO Attendance'
        ])

    output = si.getvalue()
    response = make_response(output)
    response.headers["Content-Disposition"] = f"attachment; filename=Retention_Risk_Report_{today.strftime('%Y%m%d')}.csv"
    response.headers["Content-type"] = "text/csv"

    return response

# ─────────────────────────────────────────────────────────────────────────────
# ASSIGN STUDENTS TO CLASSES (Admin Panel)
# ─────────────────────────────────────────────────────────────────────────────

@main_bp.route('/assign_students', methods=['GET', 'POST'])
@login_required
@admin_required
def assign_students():
    """Admin assigns unclassified students (class_id=None) to classes with auto-generated alphabetical / sequential roll numbers."""
    classes = get_admin_classes()

    def get_next_available_roll_number(class_id):
        """Calculates next sequential integer roll number for a class."""
        existing_students = Student.query.filter_by(class_id=class_id).all()
        max_num = 0
        import re
        for s in existing_students:
            digits = re.findall(r'\d+', str(s.roll_no or ''))
            if digits:
                try:
                    num = int(digits[-1])
                    if num > max_num:
                        max_num = num
                except Exception:
                    pass
        return max_num + 1

    if request.method == 'POST':
        action_type = request.form.get('action_type', '')

        # 1. BULK ALPHABETICAL (A-Z) / SEQUENTIAL AUTO-ROLL ASSIGNMENT
        if action_type == 'bulk_auto_roll':
            selected_student_ids = request.form.getlist('selected_student_ids')
            target_class_id = request.form.get('target_class_id')
            roll_mode = request.form.get('roll_number_mode', 'alphabetical') # 'alphabetical' or 'sequential'

            if not target_class_id or not target_class_id.isdigit():
                flash("Please select a target class to assign students.", "warning")
                return redirect(url_for('main.assign_students'))

            if not selected_student_ids:
                flash("Please select at least one student from the table.", "warning")
                return redirect(url_for('main.assign_students'))

            target_class = Class.query.get(int(target_class_id))
            if not target_class:
                flash("Selected class not found.", "danger")
                return redirect(url_for('main.assign_students'))

            student_objs = Student.query.filter(Student.id.in_([int(sid) for sid in selected_student_ids if sid.isdigit()])).all()
            if not student_objs:
                flash("No valid students found.", "warning")
                return redirect(url_for('main.assign_students'))

            # Sort students: Alphabetical (A to Z by student name) or Sequential by registration ID
            if roll_mode == 'alphabetical':
                student_objs.sort(key=lambda s: (s.name or '').strip().lower())
            else:
                student_objs.sort(key=lambda s: s.id)

            next_roll = get_next_available_roll_number(target_class.id)

            for st in student_objs:
                st.class_id = target_class.id
                if target_class.department:
                    st.department = target_class.department
                st.roll_no = str(next_roll)
                st.roll_number = str(next_roll)
                next_roll += 1

            try:
                db.session.commit()
                flash(f"✓ Successfully assigned {len(student_objs)} student(s) to '{target_class.name}' with auto-generated Roll Numbers ({roll_mode.capitalize()} order).", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Error assigning students: {e}", "danger")
            return redirect(url_for('main.assign_students'))

        # 2. Check if Range Bulk Assignment submitted
        range_start = request.form.get('range_start', '').strip()
        range_end = request.form.get('range_end', '').strip() or range_start
        range_class_id = request.form.get('range_class_id')

        if range_start and range_class_id and range_class_id.isdigit():
            target_cid = int(range_class_id)
            target_class = Class.query.get(target_cid)
            if target_class:
                all_students = get_admin_students()
                assigned_count = 0
                
                def extract_number(roll_str):
                    import re
                    digits = re.findall(r'\d+', str(roll_str or ''))
                    return int(digits[-1]) if digits else None

                start_num = extract_number(range_start)
                end_num = extract_number(range_end)

                for st in all_students:
                    st_num = extract_number(st.roll_no)
                    if st_num is not None and start_num is not None and end_num is not None:
                        if min(start_num, end_num) <= st_num <= max(start_num, end_num):
                            st.class_id = target_cid
                            if target_class.department:
                                st.department = target_class.department
                            assigned_count += 1

                if assigned_count > 0:
                    db.session.commit()
                    flash(f"Successfully assigned {assigned_count} student(s) in Roll No range [{range_start} – {range_end}] to class '{target_class.name}'.", "success")
                else:
                    flash(f"No students found in Roll No range [{range_start} – {range_end}].", "warning")
            return redirect(url_for('main.assign_students'))

        # 3. Individual / Checkbox bulk form submissions
        assigned_count = 0
        for key, value in request.form.items():
            if key.startswith('class_for_student_') and value:
                try:
                    student_id = int(key.replace('class_for_student_', ''))
                    class_id = int(value)
                    student = Student.query.get(student_id)
                    target_class = Class.query.filter(
                        Class.id == class_id,
                        (Class.admin_id == None) | (Class.admin_id == current_user.id)
                    ).first()
                    if student and target_class:
                        prev_class_id = student.class_id
                        student.class_id = class_id
                        if target_class.department:
                            student.department = target_class.department
                        # If student has no roll number or is moving to a new class, auto-assign next available roll number in that class
                        if not student.roll_no or prev_class_id != class_id:
                            next_r = get_next_available_roll_number(class_id)
                            student.roll_no = str(next_r)
                            student.roll_number = str(next_r)
                        assigned_count += 1
                except (ValueError, TypeError):
                    continue
        if assigned_count:
            db.session.commit()
            flash(f'Successfully assigned {assigned_count} student(s) to their classes with auto-generated Roll Numbers.', 'success')
        else:
            flash('No assignments were made. Please select a class for at least one student.', 'warning')
        return redirect(url_for('main.assign_students'))

    all_students = get_admin_students()
    # Auto-repair encodings for students with image_filename
    for s in all_students:
        if s.face_encoding is None and s.image_filename and face_recognition is not None:
            try:
                photo_path = os.path.join(FACES_FOLDER, s.image_filename)
                if os.path.exists(photo_path):
                    img = face_recognition.load_image_file(photo_path)
                    encs = face_recognition.face_encodings(img)
                    if encs:
                        s.face_encoding = encs[0].tobytes()
                        db.session.commit()
            except Exception as repair_err:
                print(f"Could not auto-generate encoding for student {s.id}: {repair_err}")

    unclassified_students = [s for s in all_students if s.class_id is None]
    return render_template(
        'assign_students.html',
        unclassified_students=unclassified_students,
        all_students=all_students,
        classes=classes
    )

def is_attendance_locked(attendance_date, teacher_id=None, subject_id=None):
    from datetime import datetime, time
    from extensions import get_current_date
    today = get_current_date()
    now_dt = datetime.now()

    if current_user.is_authenticated and current_user.role == 'admin':
        return False, "Admin edit access granted."

    # 1. Past dates are locked by default
    if attendance_date < today:
        if teacher_id:
            from models import AttendanceUnlockPermission
            perm = AttendanceUnlockPermission.query.filter(
                AttendanceUnlockPermission.teacher_id == teacher_id,
                AttendanceUnlockPermission.date == attendance_date,
                AttendanceUnlockPermission.unlocked_until >= datetime.utcnow()
            ).first()
            if perm:
                return False, f"Temporary unlock granted until {perm.unlocked_until.strftime('%I:%M %p')}."
        return True, f"Attendance for past date ({attendance_date.strftime('%Y-%m-%d')}) is locked."

    # 2. Today's attendance locks automatically at 11:00 PM (23:00)
    if attendance_date == today and now_dt.hour >= 23:
        if teacher_id:
            from models import AttendanceUnlockPermission
            perm = AttendanceUnlockPermission.query.filter(
                AttendanceUnlockPermission.teacher_id == teacher_id,
                AttendanceUnlockPermission.date == attendance_date,
                AttendanceUnlockPermission.unlocked_until >= datetime.utcnow()
            ).first()
            if perm:
                return False, f"Temporary unlock granted until {perm.unlocked_until.strftime('%I:%M %p')}."
        return True, "Daily attendance locks automatically at 11:00 PM."

    return False, "Attendance unlocked."

def log_attendance_action(action, attendance_id, student_id, subject_id, date_val, prev_status, new_status, rationale=None):
    try:
        from models import AttendanceAuditLog
        audit = AttendanceAuditLog(
            attendance_id=attendance_id,
            action=action,
            student_id=student_id,
            subject_id=subject_id,
            date=date_val,
            previous_status=prev_status,
            new_status=new_status,
            changed_by_user_id=current_user.id,
            changed_by_role=current_user.role if current_user.is_authenticated else 'system',
            rationale=rationale
        )
        db.session.add(audit)
        db.session.commit()
    except Exception as e:
        print(f"Error logging attendance audit: {e}")

def check_timetable_conflicts(class_id, teacher_id, day_of_week, start_time, end_time, room_number=None, exclude_slot_id=None, slot_type='CLASS'):
    if slot_type != 'CLASS':
        return False, None

    def to_minutes(t_str):
        try:
            parts = t_str.strip().split(':')
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return 0

    s_new = to_minutes(start_time)
    e_new = to_minutes(end_time)
    if e_new <= s_new:
        e_new = s_new + 50

    query = Timetable.query.filter_by(day_of_week=day_of_week)
    if exclude_slot_id:
        query = query.filter(Timetable.id != exclude_slot_id)

    existing_slots = query.all()

    for slot in existing_slots:
        if slot.slot_type != 'CLASS':
            continue
        s_exist = to_minutes(slot.start_time)
        e_exist = to_minutes(slot.end_time)
        if e_exist <= s_exist:
            e_exist = s_exist + 50

        if max(s_new, s_exist) < min(e_new, e_exist):
            if slot.class_id == class_id:
                cls_name = slot.class_assigned.name if slot.class_assigned else f"Class #{class_id}"
                sub_name = slot.subject_assigned.name if slot.subject_assigned else "another slot"
                return True, f"⚠️ Class Conflict: '{cls_name}' is already assigned on {day_of_week} ({slot.start_time}–{slot.end_time})."

            if teacher_id and slot.teacher_id == teacher_id:
                tch_name = slot.teacher_assigned.name if slot.teacher_assigned else f"Teacher #{teacher_id}"
                cls_name = slot.class_assigned.name if slot.class_assigned else f"Class #{slot.class_id}"
                return True, f"⚠️ Faculty Conflict: Teacher '{tch_name}' is already assigned to class '{cls_name}' on {day_of_week} ({slot.start_time}–{slot.end_time})."

            if room_number and slot.room_number and room_number.strip().lower() == slot.room_number.strip().lower():
                cls_name = slot.class_assigned.name if slot.class_assigned else f"Class #{slot.class_id}"
                return True, f"⚠️ Room Conflict: Room '{room_number}' is already reserved for class '{cls_name}' on {day_of_week} ({slot.start_time}–{slot.end_time})."

    return False, None

def get_or_create_period_settings():
    from models import TimetablePeriodSetting, Timetable
    settings = TimetablePeriodSetting.query.order_by(TimetablePeriodSetting.order_index).all()
    default_periods = [
        (1, 'Period 1', '09:30', '10:25', False, 1),
        (2, 'Period 2', '10:25', '11:20', False, 2),
        (0, 'Lunch Break', '11:20', '12:20', True, 3),
        (3, 'Period 3', '12:20', '01:15', False, 4),
        (4, 'Period 4', '01:15', '02:10', False, 5),
        (0, 'Short Break', '02:10', '02:30', True, 6),
        (5, 'Period 5', '02:30', '03:25', False, 7),
        (6, 'Period 6', '03:25', '04:20', False, 8)
    ]
    if not settings:
        for p_no, lbl, st, et, is_l, ord_idx in default_periods:
            ps = TimetablePeriodSetting(
                period_no=p_no,
                label=lbl,
                start_time=st,
                end_time=et,
                is_lunch=is_l,
                order_index=ord_idx
            )
            db.session.add(ps)
        db.session.commit()
        settings = TimetablePeriodSetting.query.order_by(TimetablePeriodSetting.order_index).all()
    else:
        # Check if 20-min break is missing or period timings need upgrading
        changed = False
        short_break = next((s for s in settings if s.is_lunch and 'break' in s.label.lower() and s.start_time == '02:10'), None)
        if not short_break:
            # Re-index Period 5 and 6 to make room for short break
            p5 = next((s for s in settings if s.period_no == 5), None)
            p6 = next((s for s in settings if s.period_no == 6), None)
            
            ps_break = TimetablePeriodSetting(
                period_no=0,
                label='Short Break',
                start_time='02:10',
                end_time='02:30',
                is_lunch=True,
                order_index=6
            )
            db.session.add(ps_break)
            if p5:
                p5.order_index = 7
                p5.start_time = '02:30'
                p5.end_time = '03:25'
                Timetable.query.filter_by(period_no=5).update({
                    Timetable.start_time: '02:30',
                    Timetable.end_time: '03:25'
                }, synchronize_session=False)
            if p6:
                p6.order_index = 8
                p6.start_time = '03:25'
                p6.end_time = '04:20'
                Timetable.query.filter_by(period_no=6).update({
                    Timetable.start_time: '03:25',
                    Timetable.end_time: '04:20'
                }, synchronize_session=False)
            changed = True
        else:
            p5 = next((s for s in settings if s.period_no == 5), None)
            p6 = next((s for s in settings if s.period_no == 6), None)
            if p5 and p5.start_time != '02:30':
                p5.start_time = '02:30'
                p5.end_time = '03:25'
                Timetable.query.filter_by(period_no=5).update({
                    Timetable.start_time: '02:30',
                    Timetable.end_time: '03:25'
                }, synchronize_session=False)
                changed = True
            if p6 and p6.start_time != '03:25':
                p6.start_time = '03:25'
                p6.end_time = '04:20'
                Timetable.query.filter_by(period_no=6).update({
                    Timetable.start_time: '03:25',
                    Timetable.end_time: '04:20'
                }, synchronize_session=False)
                changed = True

        if changed:
            try:
                db.session.commit()
                settings = TimetablePeriodSetting.query.order_by(TimetablePeriodSetting.order_index).all()
            except Exception:
                db.session.rollback()
    return settings


@main_bp.route('/admin/save_period_settings', methods=['POST'])
@login_required
@admin_required
def save_period_settings():
    from models import TimetablePeriodSetting, Timetable
    class_id = request.form.get('class_id')
    update_slots = request.form.get('update_existing_slots') == 'on'
    
    settings = TimetablePeriodSetting.query.order_by(TimetablePeriodSetting.order_index).all()
    
    for ps in settings:
        st = request.form.get(f'start_time_{ps.id}', '').strip()
        et = request.form.get(f'end_time_{ps.id}', '').strip()
        lbl = request.form.get(f'label_{ps.id}', '').strip()
        
        if st and et:
            ps.start_time = st
            ps.end_time = et
            if lbl:
                ps.label = lbl
            
            # If admin opted to shift all existing timetable slots for this period
            if update_slots and not ps.is_lunch:
                Timetable.query.filter_by(period_no=ps.period_no).update({
                    Timetable.start_time: st,
                    Timetable.end_time: et
                }, synchronize_session=False)
                
    db.session.commit()
    generate_daily_schedule(date.today())
    flash("✓ Timetable period timings and lunch break updated successfully!", "success")
    return redirect(url_for('main.manage_timetable', class_id=class_id))


@main_bp.route('/admin/timetable', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_timetable():
    classes = get_admin_classes()
    teachers = get_admin_teachers()
    subjects = get_admin_subjects()
    period_settings = get_or_create_period_settings()

    if request.method == 'POST':
        class_id = request.form.get('class_id')
        subject_id = request.form.get('subject_id')
        teacher_id = request.form.get('teacher_id')
        day_of_week = request.form.get('day_of_week')
        start_time = request.form.get('start_time')
        end_time = request.form.get('end_time')
        slot_type = request.form.get('slot_type', 'CLASS')
        custom_title = request.form.get('custom_title', '').strip()
        room_number = request.form.get('room_number', '').strip()
        period_val = request.form.get('period')
        eff_from_str = request.form.get('effective_from')
        eff_to_str = request.form.get('effective_to')

        if not all([class_id, day_of_week, start_time, end_time]):
            flash("Class, Day of Week, Start Time, and End Time are required.", "warning")
            return redirect(url_for('main.manage_timetable'))

        cls_id_int = int(class_id)
        sub_id_int = int(subject_id) if subject_id and subject_id.isdigit() else None
        tch_id_int = int(teacher_id) if teacher_id and teacher_id.isdigit() else None

        if slot_type == 'CLASS' and not (sub_id_int and tch_id_int):
            flash("Standard Subject lecture slots require both a Subject and a Teacher.", "warning")
            return redirect(url_for('main.manage_timetable', class_id=cls_id_int))

        if slot_type == 'LAB' and not custom_title and not sub_id_int:
            custom_title = 'Practical Laboratory'
        elif slot_type == 'LIBRARY' and not custom_title:
            custom_title = 'Library Reading'
        elif slot_type == 'OTHER' and not custom_title:
            custom_title = 'Custom Activity'

        # Check schedule conflicts
        has_conflict, err_msg = check_timetable_conflicts(
            cls_id_int, tch_id_int, day_of_week, start_time, end_time, room_number, slot_type=slot_type
        )
        if has_conflict:
            flash(err_msg, "danger")
            return redirect(url_for('main.manage_timetable', class_id=cls_id_int))

        period_int = int(period_val) if period_val and period_val.isdigit() else 1
        eff_from = datetime.strptime(eff_from_str, "%Y-%m-%d").date() if eff_from_str else date.today()
        eff_to = datetime.strptime(eff_to_str, "%Y-%m-%d").date() if eff_to_str else None

        # Check if selected subject is a Practical / Lab course or slot_type == 'LAB'
        subj_obj = Subject.query.get(sub_id_int) if sub_id_int else None
        is_practical = (slot_type == 'LAB') or (subj_obj and subj_obj.subject_type == 'Practical') or (slot_type == 'CLASS' and subj_obj and 'lab' in subj_obj.name.lower())

        # Validation Rule: Practical lab cannot be scheduled on Period 6 alone without preceding Period 5
        if is_practical:
            if period_int == 6:
                flash("⚠️ Practical / Lab classes require 2 continuous periods (~2 hours). Period 6 cannot be a standalone practical slot. Please select Period 5 (which will automatically cover Period 5 & 6).", "danger")
                return redirect(url_for('main.manage_timetable', class_id=cls_id_int))
            elif period_int not in [1, 3, 5]:
                # Advise pairing for standard period blocks (1-2, 3-4, 5-6)
                pass

        try:
            # Create primary slot
            slot = Timetable(
                class_id=cls_id_int,
                subject_id=sub_id_int,
                teacher_id=tch_id_int,
                day_of_week=day_of_week,
                period_no=period_int,
                start_time=start_time,
                end_time=end_time,
                slot_type=slot_type,
                custom_title=custom_title or None,
                room=room_number or None,
                effective_from=eff_from,
                effective_to=eff_to,
                admin_id=current_user.id
            )
            db.session.add(slot)

            # If Practical Lab: Automatically create or pair the consecutive second period (e.g. 1->2, 3->4, 5->6)
            paired_p_no = None
            if is_practical and period_int in [1, 2, 3, 4, 5]:
                paired_p_no = period_int + 1
                paired_ps = next((s for s in period_settings if s.period_no == paired_p_no and not s.is_lunch), None)
                if paired_ps:
                    # Remove any conflicting single slot on the paired period in target class
                    Timetable.query.filter_by(
                        class_id=cls_id_int,
                        day_of_week=day_of_week,
                        period_no=paired_p_no
                    ).delete(synchronize_session=False)

                    slot2 = Timetable(
                        class_id=cls_id_int,
                        subject_id=sub_id_int,
                        teacher_id=tch_id_int,
                        day_of_week=day_of_week,
                        period_no=paired_p_no,
                        start_time=paired_ps.start_time,
                        end_time=paired_ps.end_time,
                        slot_type=slot_type,
                        custom_title=custom_title or None,
                        room=room_number or None,
                        effective_from=eff_from,
                        effective_to=eff_to,
                        admin_id=current_user.id
                    )
                    db.session.add(slot2)

            db.session.commit()
            generate_daily_schedule(date.today())

            lab_name = subj_obj.name if subj_obj else (custom_title or 'Lab')
            if is_practical and paired_p_no:
                flash(f"✓ Practical / Lab ({lab_name}) successfully allocated for 2 continuous class periods (Period {period_int} & Period {paired_p_no} — ~2 Hours total)!", "success")
            else:
                flash(f"Timetable slot ({slot_type if slot_type != 'OTHER' else custom_title}) created successfully!", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating timetable slot: {e}", "danger")

        return redirect(url_for('main.manage_timetable', class_id=cls_id_int))

    selected_department = request.args.get('department', '').strip()
    selected_class_id_str = request.args.get('class_id')
    selected_class_id = None
    
    # Retrieve all registered departments from Department table or Class entities
    all_departments = Department.query.order_by(Department.name.asc()).all()
    if not all_departments:
        dept_names = sorted(list(set(c.department for c in classes if c.department)))
        all_departments = [type('DeptObj', (object,), {'name': d, 'code': d})() for d in dept_names]

    # Filter classes belonging to selected department
    if selected_department:
        dept_classes = [c for c in classes if c.department and c.department.lower() == selected_department.lower()]
    else:
        dept_classes = classes

    if selected_class_id_str:
        try:
            cid = int(selected_class_id_str)
            if any(c.id == cid for c in dept_classes):
                selected_class_id = cid
            elif dept_classes:
                selected_class_id = dept_classes[0].id
        except ValueError:
            pass
    elif dept_classes:
        selected_class_id = dept_classes[0].id
    else:
        selected_class_id = None

    selected_class = Class.query.get(selected_class_id) if selected_class_id else None
    if selected_class and not selected_department and selected_class.department:
        selected_department = selected_class.department

    if selected_class_id:
        timetable_entries = Timetable.query.filter_by(class_id=selected_class_id).order_by(Timetable.day_of_week, Timetable.start_time).all()
    else:
        timetable_entries = []

    declared_holidays = Holiday.query.order_by(Holiday.date.desc()).limit(30).all()

    return render_template(
        'manage_timetable.html',
        timetable_entries=timetable_entries,
        classes=classes,
        dept_classes=dept_classes,
        teachers=teachers,
        subjects=subjects,
        period_settings=period_settings,
        all_departments=all_departments,
        selected_department=selected_department,
        selected_class_id=selected_class_id,
        selected_class=selected_class,
        declared_holidays=declared_holidays
    )

@main_bp.route('/admin/delete_timetable/<int:slot_id>', methods=['POST', 'GET'])
@login_required
@admin_required
def delete_timetable_slot(slot_id):
    slot = Timetable.query.get_or_404(slot_id)
    class_id = slot.class_id
    try:
        # Also clean up any paired duplicate slots if this was a 2-period lab slot
        is_lab = slot.slot_type in ['LAB', 'PRACTICAL'] or (slot.subject_assigned and slot.subject_assigned.subject_type == 'Practical')
        dow = slot.day_of_week
        p_no = slot.period_no
        s_id = slot.subject_id
        c_title = slot.custom_title

        db.session.delete(slot)

        # If it was part of a paired lab slot on period 1-2, 3-4, or 5-6, remove paired sibling if requested
        if is_lab and p_no:
            sibling_p = (p_no + 1) if p_no in [1, 3, 5] else ((p_no - 1) if p_no in [2, 4, 6] else None)
            if sibling_p:
                sibling_slot = Timetable.query.filter_by(
                    class_id=class_id,
                    day_of_week=dow,
                    period_no=sibling_p
                ).first()
                if sibling_slot and (sibling_slot.slot_type in ['LAB', 'PRACTICAL'] or sibling_slot.subject_id == s_id or sibling_slot.custom_title == c_title):
                    db.session.delete(sibling_slot)

        db.session.commit()
        generate_daily_schedule(date.today())
        flash("Timetable slot removed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting timetable slot: {e}", "danger")
    return redirect(url_for('main.manage_timetable', class_id=class_id))

@main_bp.route('/admin/edit_timetable/<int:slot_id>', methods=['POST'])
@login_required
@admin_required
def edit_timetable_slot(slot_id):
    slot = Timetable.query.get_or_404(slot_id)
    class_id = request.form.get('class_id')
    subject_id = request.form.get('subject_id')
    teacher_id = request.form.get('teacher_id')
    day_of_week = request.form.get('day_of_week')
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')
    slot_type = request.form.get('slot_type', slot.slot_type or 'CLASS')
    custom_title = request.form.get('custom_title', '').strip()
    room_number = request.form.get('room_number', '').strip()
    period_val = request.form.get('period')
    eff_from_str = request.form.get('effective_from')
    eff_to_str = request.form.get('effective_to')

    c_id = int(class_id) if class_id else slot.class_id
    s_id = int(subject_id) if subject_id and subject_id.isdigit() else (None if slot_type not in ['CLASS', 'LAB'] else slot.subject_id)
    t_id = int(teacher_id) if teacher_id and teacher_id.isdigit() else (None if slot_type not in ['CLASS', 'LAB', 'LIBRARY', 'OTHER'] else (int(teacher_id) if teacher_id and teacher_id.isdigit() else None))
    dow = day_of_week or slot.day_of_week
    st = start_time or slot.start_time
    et = end_time or slot.end_time

    if slot_type == 'LAB' and not custom_title and not s_id:
        custom_title = slot.custom_title or 'Practical Laboratory'
    elif slot_type == 'LIBRARY' and not custom_title:
        custom_title = 'Library Reading'
    elif slot_type == 'OTHER' and not custom_title:
        custom_title = slot.custom_title or 'Custom Activity'

    # Conflict check
    has_conflict, err_msg = check_timetable_conflicts(
        c_id, t_id, dow, st, et, room_number, exclude_slot_id=slot.id, slot_type=slot_type
    )
    if has_conflict:
        flash(err_msg, "danger")
        return redirect(url_for('main.manage_timetable', class_id=c_id))

    try:
        slot.class_id = c_id
        slot.subject_id = s_id
        slot.teacher_id = t_id
        slot.day_of_week = dow
        slot.start_time = st
        slot.end_time = et
        slot.slot_type = slot_type
        slot.custom_title = custom_title or None
        if period_val and period_val.isdigit():
            slot.period_no = int(period_val)
        slot.room = room_number or None
        if eff_from_str:
            slot.effective_from = datetime.strptime(eff_from_str, "%Y-%m-%d").date()
        if eff_to_str:
            slot.effective_to = datetime.strptime(eff_to_str, "%Y-%m-%d").date()
        
        db.session.commit()
        generate_daily_schedule(date.today())
        flash(f"Timetable slot ({slot_type if slot_type != 'OTHER' else custom_title}) updated successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating timetable slot: {e}", "danger")
    return redirect(url_for('main.manage_timetable', class_id=c_id))

@main_bp.route('/admin/copy_timetable', methods=['POST'])
@login_required
@admin_required
def copy_timetable():
    from models import Timetable
    source_class_id = request.form.get('source_class_id')
    target_class_id = request.form.get('target_class_id')
    overwrite = request.form.get('overwrite') == 'true'

    if not source_class_id or not target_class_id:
        flash("Source Class and Target Class are required to copy timetable.", "warning")
        return redirect(url_for('main.manage_timetable'))

    if source_class_id == target_class_id:
        flash("Source and Target class cannot be the same.", "warning")
        return redirect(url_for('main.manage_timetable'))

    source_slots = Timetable.query.filter_by(class_id=int(source_class_id)).all()
    if not source_slots:
        flash("Source class has no timetable slots to copy.", "warning")
        return redirect(url_for('main.manage_timetable'))

    try:
        if overwrite:
            Timetable.query.filter_by(class_id=int(target_class_id)).delete()

        copied_count = 0
        for src in source_slots:
            new_slot = Timetable(
                class_id=int(target_class_id),
                subject_id=src.subject_id,
                teacher_id=src.teacher_id,
                day_of_week=src.day_of_week,
                period_no=src.period_no,
                start_time=src.start_time,
                end_time=src.end_time,
                slot_type=src.slot_type or 'CLASS',
                custom_title=src.custom_title,
                room=src.room,
                effective_from=src.effective_from,
                effective_to=src.effective_to,
                admin_id=current_user.id
            )
            db.session.add(new_slot)
            copied_count += 1

        db.session.commit()
        generate_daily_schedule(date.today())
        flash(f"Successfully copied {copied_count} timetable slot(s) with all period numbers to target class!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error copying timetable: {e}", "danger")

    return redirect(url_for('main.manage_timetable', class_id=int(target_class_id)))

@main_bp.route('/admin/autofill_timetable', methods=['POST'])
@login_required
@admin_required
def autofill_timetable():
    from models import Timetable, Subject, Teacher, Class
    class_id = request.form.get('class_id')
    subject_id = request.form.get('subject_id')
    teacher_id = request.form.get('teacher_id')

    if not class_id:
        flash("Please select a target class to auto-fill slots.", "warning")
        return redirect(url_for('main.manage_timetable'))

    cls = Class.query.get(int(class_id))
    if not cls:
        flash("Target class not found.", "danger")
        return redirect(url_for('main.manage_timetable'))

    class_subjects = Subject.query.filter_by(class_id=cls.id).all()
    all_teachers = Teacher.query.all()

    if not class_subjects and not subject_id:
        flash("No subjects found for this class. Please add subjects to the class first.", "warning")
        return redirect(url_for('main.manage_timetable'))

    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    time_slots = [
        ('09:00', '10:00'),
        ('10:00', '11:00'),
        ('11:00', '12:00'),
        ('13:00', '14:00'),
        ('14:00', '15:00'),
        ('15:00', '16:00')
    ]

    try:
        created_count = 0
        sub_idx = 0

        for day in days:
            for start_t, end_t in time_slots:
                exists = Timetable.query.filter_by(
                    class_id=cls.id,
                    day_of_week=day,
                    start_time=start_t
                ).first()
                if not exists:
                    if subject_id and str(subject_id).isdigit():
                        curr_sub = Subject.query.get(int(subject_id))
                    elif class_subjects:
                        curr_sub = class_subjects[sub_idx % len(class_subjects)]
                        sub_idx += 1
                    else:
                        curr_sub = None

                    if teacher_id and str(teacher_id).isdigit():
                        curr_t_id = int(teacher_id)
                    elif curr_sub and curr_sub.teacher_id:
                        curr_t_id = curr_sub.teacher_id
                    elif all_teachers:
                        curr_t_id = all_teachers[0].id
                    else:
                        curr_t_id = None

                    if curr_sub and curr_t_id:
                        slot = Timetable(
                            class_id=cls.id,
                            subject_id=curr_sub.id,
                            teacher_id=curr_t_id,
                            day_of_week=day,
                            start_time=start_t,
                            end_time=end_t,
                            room_number=f'Room {cls.name}',
                            admin_id=current_user.id
                        )
                        db.session.add(slot)
                        created_count += 1

        db.session.commit()
        flash(f"Successfully auto-filled {created_count} empty slot(s) for class {cls.name}!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error during auto-fill: {e}", "danger")

    return redirect(url_for('main.manage_timetable'))

@main_bp.route('/admin/clear_timetable', methods=['POST'])
@login_required
@admin_required
def clear_timetable():
    from models import Timetable
    class_id = request.form.get('class_id')
    if not class_id:
        flash("Please select a class to clear timetable slots.", "warning")
        return redirect(url_for('main.manage_timetable'))

    try:
        deleted = Timetable.query.filter_by(class_id=int(class_id)).delete()
        db.session.commit()
        flash(f"Cleared {deleted} timetable slot(s) for the selected class.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error clearing timetable: {e}", "danger")

    return redirect(url_for('main.manage_timetable'))

@main_bp.route('/admin/assign_class_teacher_direct', methods=['POST'])
@login_required
@admin_required
def assign_class_teacher_direct():
    from models import Class, Teacher
    class_id = request.form.get('class_id')
    teacher_id = request.form.get('teacher_id')

    if not class_id:
        flash("Please select a target class.", "warning")
        return redirect(url_for('main.manage_classes'))

    cls = Class.query.get_or_404(int(class_id))
    if teacher_id and str(teacher_id).isdigit():
        t = Teacher.query.get(int(teacher_id))
        cls.class_teacher_id = t.id if t else None
        t_name = t.name if t else "Unassigned"
    else:
        cls.class_teacher_id = None
        t_name = "Unassigned"

    try:
        db.session.commit()
        flash(f"Updated Designated Class Teacher for {cls.name} to {t_name}.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error assigning Class Teacher: {e}", "danger")

    return redirect(url_for('main.manage_classes'))

@main_bp.route('/admin/attendance_unlock', methods=['POST'])
@login_required
@admin_required
def attendance_unlock():
    from datetime import datetime, timedelta
    from models import AttendanceUnlockPermission
    teacher_id = request.form.get('teacher_id')
    subject_id = request.form.get('subject_id')
    date_str = request.form.get('date')
    duration_mins = int(request.form.get('duration_minutes', 120))

    if not teacher_id or not date_str:
        flash("Teacher and Date are required for temporary unlock.", "warning")
        return redirect(url_for('main.view_reports'))

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        unlocked_until = datetime.utcnow() + timedelta(minutes=duration_mins)

        perm = AttendanceUnlockPermission(
            teacher_id=int(teacher_id),
            subject_id=int(subject_id) if subject_id and subject_id.isdigit() else None,
            date=target_date,
            unlocked_until=unlocked_until,
            granted_by_admin_id=current_user.id
        )
        db.session.add(perm)
        db.session.commit()

        log_attendance_action(
            action='UNLOCK',
            attendance_id=None,
            student_id=None,
            subject_id=perm.subject_id,
            date_val=target_date,
            prev_status='LOCKED',
            new_status='UNLOCKED',
            rationale=f"Granted temporary unlock permission for {duration_mins} mins."
        )

        flash(f"Temporary editing permission granted for Teacher ID {teacher_id} until {unlocked_until.strftime('%I:%M %p UTC')}!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error granting unlock permission: {e}", "danger")

    return redirect(url_for('main.view_reports'))

@main_bp.route('/admin/audit_logs')
@login_required
@admin_required
def audit_logs():
    from models import AttendanceAuditLog
    logs = AttendanceAuditLog.query.order_by(AttendanceAuditLog.timestamp.desc()).limit(200).all()
    return render_template('attendance_audit_logs.html', logs=logs)

@main_bp.route('/admin/recommend_teachers', methods=['GET', 'POST'])
@login_required
@admin_required
def recommend_teachers():
    classes = get_admin_classes()
    teachers = get_admin_teachers()
    
    target_subject_name = request.args.get('subject_name', '').strip()
    target_class_id = request.args.get('class_id')

    # Handle Admin Approval & Assignment
    if request.method == 'POST':
        selected_teacher_id = request.form.get('teacher_id')
        sub_name = request.form.get('subject_name', '').strip()
        cid = request.form.get('class_id')

        if not selected_teacher_id or not sub_name or not cid:
            flash("Teacher, Subject Name, and Class selection are required.", "warning")
            return redirect(url_for('main.recommend_teachers'))

        try:
            # Check if subject already exists for class
            existing_sub = Subject.query.filter_by(name=sub_name, class_id=int(cid)).first()
            if existing_sub:
                existing_sub.teacher_id = int(selected_teacher_id)
                flash(f"Updated subject '{sub_name}' assignment to recommended teacher ID {selected_teacher_id}.", "success")
            else:
                new_sub = Subject(
                    name=sub_name,
                    class_id=int(cid),
                    teacher_id=int(selected_teacher_id),
                    admin_id=current_user.id
                )
                db.session.add(new_sub)
                flash(f"Approved recommendation! Created and assigned '{sub_name}' to teacher ID {selected_teacher_id}.", "success")
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Error executing assignment: {e}", "danger")
            
        return redirect(url_for('main.manage_subjects'))

    recommendations = []

    if target_subject_name and target_class_id:
        target_cid = int(target_class_id)
        target_class = Class.query.get(target_cid)

        for t in teachers:
            score = 0
            reasons = []

            # 1. Primary Subject Match
            if t.primary_subject and target_subject_name.lower() in t.primary_subject.lower():
                score += 40
                reasons.append("Primary Teaching Expertise Match (+40 pts)")
            # 2. Secondary Subject Match
            elif t.secondary_subject and target_subject_name.lower() in t.secondary_subject.lower():
                score += 25
                reasons.append("Secondary Subject Match (+25 pts)")
            # 3. Tertiary Subject Choice
            elif t.tertiary_subject and target_subject_name.lower() in t.tertiary_subject.lower():
                score += 10
                reasons.append("3rd Preference Match (+10 pts)")

            # 4. Status / Experience
            if t.status == 'Approved':
                score += 15
                reasons.append("Approved Faculty Status (+15 pts)")

            # 5. Workload Score
            taught_count = len(t.subjects)
            wl_score = max(0, 30 - taught_count * 10)
            score += wl_score
            reasons.append(f"Workload Balance ({taught_count} subjects taught, +{wl_score} pts)")

            # 6. Availability Check
            from models import Timetable
            timetable_slots = Timetable.query.filter_by(teacher_id=t.id).count()
            avail_score = max(0, 20 - timetable_slots * 2)
            score += avail_score
            reasons.append(f"Timetable Availability ({timetable_slots} slots booked, +{avail_score} pts)")

            recommendations.append({
                'teacher': t,
                'score': score,
                'reasons': reasons,
                'current_workload': taught_count,
                'primary': t.primary_subject or 'Not Set',
                'secondary': t.secondary_subject or 'Not Set'
            })

        # Rank by score descending
        recommendations.sort(key=lambda x: x['score'], reverse=True)

    return render_template(
        'recommend_teachers.html',
        classes=classes,
        recommendations=recommendations,
        target_subject_name=target_subject_name,
        target_class_id=int(target_class_id) if target_class_id and target_class_id.isdigit() else None
    )

@main_bp.route('/admin/teacher_assignments', methods=['GET', 'POST'])
@login_required
@admin_required
def teacher_assignments():
    teachers = get_admin_teachers()
    classes = get_admin_classes()
    subjects = get_admin_subjects()

    if request.method == 'POST':
        teacher_id = request.form.get('teacher_id')
        class_id = request.form.get('class_id')
        subject_id = request.form.get('subject_id')

        if not all([teacher_id, class_id, subject_id]):
            flash("Teacher, Class, and Subject are required for assignment.", "warning")
            return redirect(url_for('main.teacher_assignments'))

        t_id = int(teacher_id)
        c_id = int(class_id)
        s_id = int(subject_id)

        existing = TeacherAssignment.query.filter_by(teacher_id=t_id, class_id=c_id, subject_id=s_id).first()
        if existing:
            flash("This teacher assignment already exists.", "info")
        else:
            try:
                asn = TeacherAssignment(teacher_id=t_id, class_id=c_id, subject_id=s_id)
                db.session.add(asn)
                db.session.commit()
                flash("Teacher assignment created successfully!", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Error creating assignment: {e}", "danger")
        return redirect(url_for('main.teacher_assignments'))

    assignments_list = TeacherAssignment.query.all()
    return render_template(
        'teacher_assignments.html',
        assignments=assignments_list,
        teachers=teachers,
        classes=classes,
        subjects=subjects
    )

@main_bp.route('/admin/delete_teacher_assignment/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_teacher_assignment(id):
    asn = TeacherAssignment.query.get_or_404(id)
    try:
        db.session.delete(asn)
        db.session.commit()
        flash("Teacher assignment removed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error removing assignment: {e}", "danger")
    return redirect(url_for('main.teacher_assignments'))

@main_bp.route('/admin/holidays', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_holidays():
    classes = get_admin_classes()

    if request.method == 'POST':
        date_str = request.form.get('date')
        scope = request.form.get('scope', 'ALL')
        reason = request.form.get('reason', '').strip()

        if not date_str:
            flash("Holiday date is required.", "warning")
            return redirect(url_for('main.manage_holidays'))

        h_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        try:
            hol = Holiday(date=h_date, scope=scope, reason=reason)
            db.session.add(hol)
            db.session.commit()

            # Update daily schedule for this date
            generate_daily_schedule(h_date)

            flash(f"Holiday on {h_date} saved successfully!", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating holiday: {e}", "danger")

        return redirect(url_for('main.manage_holidays'))

    holidays_list = Holiday.query.order_by(Holiday.date.desc()).all()
    return render_template('manage_holidays.html', holidays=holidays_list, classes=classes)

@main_bp.route('/admin/delete_holiday/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_holiday(id):
    hol = Holiday.query.get_or_404(id)
    h_date = hol.date
    try:
        db.session.delete(hol)
        db.session.commit()
        generate_daily_schedule(h_date)
        flash("Holiday removed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error removing holiday: {e}", "danger")
    return redirect(request.referrer or url_for('main.manage_holidays'))

@main_bp.route('/admin/declare_college_off', methods=['POST'])
@login_required
@admin_required
def declare_college_off():
    date_from_str = request.form.get('date_from') or request.form.get('date')
    date_to_str = request.form.get('date_to') or date_from_str
    scope = request.form.get('scope', 'ALL').strip()
    reason_category = request.form.get('reason_category', 'Emergency Incident').strip()
    custom_reason = request.form.get('reason', '').strip()
    broadcast_notice = request.form.get('broadcast_notice') == '1' or request.form.get('broadcast_notice') == 'on'

    final_reason = f"{reason_category}: {custom_reason}" if custom_reason else reason_category

    if not date_from_str:
        flash("Date is required to declare college off.", "warning")
        return redirect(request.referrer or url_for('main.manage_timetable'))

    try:
        d_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
        d_to = datetime.strptime(date_to_str, "%Y-%m-%d").date() if date_to_str else d_from
    except ValueError:
        flash("Invalid date format provided.", "danger")
        return redirect(request.referrer or url_for('main.manage_timetable'))

    if d_to < d_from:
        d_to = d_from

    from datetime import timedelta
    curr = d_from
    days_count = 0
    all_teachers = Teacher.query.all()

    try:
        while curr <= d_to:
            # 1. Create or update Holiday record
            existing_hol = Holiday.query.filter_by(date=curr, scope=scope).first()
            if not existing_hol:
                existing_hol = Holiday(date=curr, scope=scope, reason=final_reason)
                db.session.add(existing_hol)
            else:
                existing_hol.reason = final_reason

            # 2. Regenerate daily lecture schedule for this date (sets resolved_status='HOLIDAY')
            generate_daily_schedule(curr)

            # 3. Synchronize Teacher Daily Attendance (Only if FULL College Off for faculty too)
            is_faculty_working_day = (scope in ('CLASSES_ONLY', 'STUDENTS_ONLY'))
            if not is_faculty_working_day and (scope == 'ALL' or str(scope).upper() == 'ALL'):
                for t in all_teachers:
                    rec = TeacherDailyAttendance.query.filter_by(teacher_id=t.id, attendance_date=curr).first()
                    if not rec:
                        rec = TeacherDailyAttendance(teacher_id=t.id, attendance_date=curr, status='Holiday')
                        db.session.add(rec)
                    else:
                        rec.status = 'Holiday'
                        rec.is_uninformed_absence = False
                        rec.late_status = 'Holiday'
                        rec.absence_reason = final_reason

            days_count += 1
            curr += timedelta(days=1)

        # 4. If broadcast notice requested, create institutional notices for students & faculty
        if broadcast_notice:
            target_class_id = int(scope) if scope.isdigit() else None
            date_label = d_from.strftime('%b %d, %Y') if d_from == d_to else f"{d_from.strftime('%b %d, %Y')} to {d_to.strftime('%b %d, %Y')}"
            
            if is_faculty_working_day:
                # Student Notice (Classes suspended)
                stu_title = f"📢 STUDENT NOTICE: Classes Suspended ({date_label})"
                stu_content = f"Official Administration Notice: Regular student lectures and labs are suspended on {date_label}.\n\nReason / Event: {final_reason}.\nStudents are not required to attend classes today."
                stu_ann = ClassAnnouncement(
                    class_id=target_class_id,
                    admin_id=current_user.id,
                    posted_by_role='admin',
                    target_role='STUDENTS',
                    title=stu_title,
                    content=stu_content,
                    notice_type='Emergency Notice'
                )
                db.session.add(stu_ann)

                # Faculty Notice (Teachers must report)
                fac_title = f"📋 FACULTY DUTY NOTICE: Classes Suspended - Faculty Working Day ({date_label})"
                fac_content = f"Official Administration Notice: Student classes are suspended on {date_label} for '{final_reason}'.\n\nAll faculty members and staff are REQUIRED on duty / regular working hours. Please mark your daily morning & evening attendance as normal."
                fac_ann = ClassAnnouncement(
                    class_id=None,
                    admin_id=current_user.id,
                    posted_by_role='admin',
                    target_role='TEACHERS',
                    title=fac_title,
                    content=fac_content,
                    notice_type='Emergency Notice'
                )
                db.session.add(fac_ann)
            else:
                # Full college / global closure
                notice_title = f"🚨 EMERGENCY NOTICE: College Off / Classes Suspended ({date_label})"
                notice_content = f"Official Administration Notice: College is closed / classes are suspended for {date_label}.\n\nReason: {final_reason}.\nAll scheduled lectures, practical labs, and attendance requirements are suspended for this period."
                announcement = ClassAnnouncement(
                    class_id=target_class_id,
                    admin_id=current_user.id,
                    posted_by_role='admin',
                    target_role='ALL',
                    title=notice_title,
                    content=notice_content,
                    notice_type='Emergency Notice'
                )
                db.session.add(announcement)

        db.session.commit()
        date_desc = d_from.strftime('%b %d, %Y') if d_from == d_to else f"{d_from.strftime('%b %d, %Y')} to {d_to.strftime('%b %d, %Y')}"
        mode_label = "Students / Classes Off (Faculty Working Day)" if is_faculty_working_day else "Full College Closure"
        flash(f"🚨 {mode_label} successfully declared for {date_desc} ({days_count} day(s)). Timetable slots and notices synchronized.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error declaring college off: {e}", "danger")

    return redirect(request.referrer or url_for('main.manage_timetable'))

@main_bp.route('/admin/leave_approval/<int:leave_id>/<action>', methods=['POST'])
@login_required
@admin_required
def handle_leave_approval(leave_id, action):
    leave = TeacherLeave.query.get_or_404(leave_id)
    substitute_id_str = request.form.get('substitute_teacher_id')
    broadcast_cancel_notice = request.form.get('broadcast_cancel_notice') == '1'
    custom_cancel_message = request.form.get('custom_cancel_message', '').strip()

    if action == 'approve':
        leave.status = 'APPROVED'
        if substitute_id_str and substitute_id_str.isdigit():
            leave.substitute_teacher_id = int(substitute_id_str)
        else:
            leave.substitute_teacher_id = None

        # 1. Synchronize Teacher Daily Attendance records for the leave period
        try:
            from datetime import timedelta
            from models import TeacherDailyAttendance, ClassAnnouncement, Timetable
            from teacher_attendance.routes import recalculate_daily_status, get_or_create_settings
            settings = get_or_create_settings()

            curr = leave.date_from
            while curr <= leave.date_to:
                rec = TeacherDailyAttendance.query.filter_by(teacher_id=leave.teacher_id, attendance_date=curr).first()
                if not rec:
                    rec = TeacherDailyAttendance(teacher_id=leave.teacher_id, attendance_date=curr)
                    db.session.add(rec)
                recalculate_daily_status(rec, settings)
                curr += timedelta(days=1)

            # 2. If No Substitute is assigned and broadcast switch is ON, post notice to affected student classes
            if not leave.substitute_teacher_id and broadcast_cancel_notice:
                # Find all classes taught by this teacher
                classes_taught = db.session.query(Timetable.class_id).filter_by(teacher_id=leave.teacher_id).distinct().all()
                class_ids = [c[0] for c in classes_taught if c[0]]
                
                notice_text = custom_cancel_message or f"Notice: Prof. {leave.teacher.name} is on sanctioned leave from {leave.date_from.strftime('%d %b')} to {leave.date_to.strftime('%d %b')}. Scheduled lectures are cancelled for this duration."
                notice_title = f"Class Cancellation: Prof. {leave.teacher.name} On Leave"

                for cid in class_ids:
                    announcement = ClassAnnouncement(
                        class_id=cid,
                        teacher_id=leave.teacher_id,
                        admin_id=current_user.id,
                        posted_by_role='admin',
                        target_role='STUDENTS',
                        title=notice_title,
                        content=notice_text,
                        notice_type='Urgent Notice'
                    )
                    db.session.add(announcement)
        except Exception as sync_err:
            print(f"[Leave Approval Sync Warning] {sync_err}")

        sub_name = leave.substitute_teacher.name if leave.substitute_teacher else 'None (Class Cancelled & Students Notified)'
        flash(f"Leave request for teacher '{leave.teacher.name}' APPROVED (Substitute: {sub_name}). Marked as Approved Leave on teacher attendance log.", "success")
    elif action == 'reject':
        leave.status = 'REJECTED'
        flash(f"Leave request for teacher '{leave.teacher.name}' REJECTED.", "info")

    try:
        db.session.commit()
        # Regenerate daily schedule for the leave range
        from datetime import timedelta
        curr = leave.date_from
        while curr <= leave.date_to:
            generate_daily_schedule(curr)
            curr += timedelta(days=1)
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating leave status: {e}", "danger")

    return redirect(url_for('main.approvals'))

@main_bp.route('/admin/override_leave', methods=['POST'])
@login_required
@admin_required
def override_leave():
    leave_id = request.form.get('leave_id', type=int)
    new_status = request.form.get('status', 'APPROVED').upper().strip()
    substitute_id_str = request.form.get('substitute_teacher_id')
    reason = request.form.get('reason', 'Admin override').strip()

    leave = TeacherLeave.query.get_or_404(leave_id)
    prev_status = leave.status
    leave.status = new_status

    if substitute_id_str and substitute_id_str.isdigit():
        leave.substitute_teacher_id = int(substitute_id_str)
    elif substitute_id_str == '' or substitute_id_str == 'none':
        leave.substitute_teacher_id = None

    try:
        db.session.commit()
        # Regenerate daily schedule for the leave range
        from datetime import timedelta
        curr = leave.date_from
        while curr <= leave.date_to:
            generate_daily_schedule(curr)
            curr += timedelta(days=1)
        sub_name = leave.substitute_teacher.name if leave.substitute_teacher else 'Auto / Unassigned'
        flash(f"✓ Leave for '{leave.teacher.name}' updated to {new_status} (Substitute: {sub_name}). Schedules synchronized.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error overriding leave status: {e}", "danger")

    return redirect(request.referrer or url_for('main.approvals'))

@main_bp.route('/admin/correction_approval/<int:req_id>/<action>', methods=['POST'])
@login_required
@admin_required
def handle_correction_approval(req_id, action):
    corr = CorrectionRequest.query.get_or_404(req_id)
    target_student_id = request.form.get('student_id')
    new_status = request.form.get('status', 'PRESENT').upper() # 'PRESENT' or 'ABSENT'

    if action == 'approve':
        corr.status = 'APPROVED'
        corr.reviewed_by = current_user.id
        corr.reviewed_at = datetime.utcnow()

        if target_student_id and target_student_id.isdigit():
            st_id = int(target_student_id)
            rec = AttendanceRecord.query.filter_by(session_id=corr.session_id, student_id=st_id).first()
            prev_st = rec.status if rec else 'NONE'
            if not rec:
                rec = AttendanceRecord(session_id=corr.session_id, student_id=st_id, status=new_status, marked_by='MANUAL')
                db.session.add(rec)
            else:
                rec.status = new_status
                rec.marked_by = 'MANUAL'
                rec.marked_at = datetime.utcnow()

            # Audit log
            audit = AttendanceAuditLog(
                action='CORRECTION_APPROVED',
                student_id=st_id,
                subject_id=corr.session.subject_id,
                date=corr.session.date,
                previous_status=prev_st,
                new_status=new_status,
                changed_by_user_id=current_user.id,
                changed_by_role='admin',
                rationale=corr.reason
            )
            db.session.add(audit)

        flash("Attendance correction request APPROVED and applied.", "success")
    elif action == 'reject':
        corr.status = 'REJECTED'
        corr.reviewed_by = current_user.id
        corr.reviewed_at = datetime.utcnow()
        flash("Attendance correction request REJECTED.", "info")

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"Error processing correction request: {e}", "danger")

    return redirect(url_for('main.approvals'))

@main_bp.route('/admin/announcements', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_announcements():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        target_role = request.form.get('target_role', 'ALL') # 'TEACHERS', 'STUDENTS', 'ALL'
        notice_type = request.form.get('notice_type', 'Announcement')
        class_id = request.form.get('class_id')

        if not title or not content:
            flash("Title and Content are required.", "warning")
            return redirect(url_for('main.manage_announcements'))

        ann = ClassAnnouncement(
            title=title,
            content=content,
            notice_type=notice_type,
            target_role=target_role,
            posted_by_role='admin',
            admin_id=current_user.id,
            class_id=int(class_id) if class_id and class_id != 'ALL' and target_role in ['STUDENTS', 'ALL'] else None,
            created_at=datetime.utcnow()
        )
        db.session.add(ann)
        db.session.commit()
        flash(f"✓ Announcement successfully published to {target_role.title()}!", "success")
        return redirect(url_for('main.manage_announcements'))

    # GET requests - Only show Admin's own institutional announcements
    announcements_list = ClassAnnouncement.query.filter_by(posted_by_role='admin').order_by(ClassAnnouncement.created_at.desc()).all()
    classes = Class.query.order_by(Class.name).all()

    return render_template(
        'admin_announcements.html',
        announcements=announcements_list,
        classes=classes
    )

@main_bp.route('/admin/announcement/delete/<int:ann_id>', methods=['POST'])
@login_required
@admin_required
def delete_announcement(ann_id):
    ann = ClassAnnouncement.query.get_or_404(ann_id)
    db.session.delete(ann)
    db.session.commit()
    flash("✓ Announcement permanently deleted from portal and all feeds.", "info")
    return redirect(url_for('main.manage_announcements'))

@main_bp.route('/admin/id_cards')
@login_required
@admin_required
def admin_id_cards():
    classes = get_admin_classes()
    departments = Department.query.order_by(Department.name.asc()).all()
    teachers = Teacher.query.filter_by(status='Approved').order_by(Teacher.name.asc()).all()
    
    card_type = request.args.get('type', 'students') # 'students' or 'teachers'
    selected_dept = request.args.get('department', '').strip()
    selected_class_id = request.args.get('class_id')
    selected_status = request.args.get('status', '').strip() # 'Active' or 'Suspended'
    search_query = request.args.get('q', '').strip()

    # Filter classes by selected department if provided
    filtered_classes = classes
    if selected_dept:
        filtered_classes = [c for c in classes if (c.department and c.department.strip() == selected_dept) or (selected_dept in (c.name or ''))]

    students = []
    if card_type == 'students':
        q = Student.query
        if selected_class_id and selected_class_id.isdigit():
            q = q.filter_by(class_id=int(selected_class_id))
        elif selected_dept:
            class_ids_in_dept = [c.id for c in filtered_classes]
            q = q.filter((Student.class_id.in_(class_ids_in_dept)) | (Student.department == selected_dept))
        
        if selected_status == 'Active':
            q = q.filter((Student.is_suspended == False) | (Student.is_suspended == None))
        elif selected_status == 'Suspended':
            q = q.filter(Student.is_suspended == True)

        if search_query:
            q = q.filter((Student.name.ilike(f"%{search_query}%")) | (Student.roll_no.ilike(f"%{search_query}%")) | (Student.enrollment_no.ilike(f"%{search_query}%")))
        students = q.order_by(Student.roll_no.asc()).all()
    else:
        if selected_dept:
            teachers = [t for t in teachers if t.effective_department == selected_dept or (t.department and t.department.strip() == selected_dept)]
        
        if selected_status == 'Active':
            teachers = [t for t in teachers if not t.is_suspended]
        elif selected_status == 'Suspended':
            teachers = [t for t in teachers if t.is_suspended]

        if search_query:
            teachers = [t for t in teachers if search_query.lower() in t.name.lower() or (t.emp_id and search_query.lower() in t.emp_id.lower()) or (t.department and search_query.lower() in t.department.lower())]

    return render_template(
        'admin_id_cards.html',
        classes=filtered_classes,
        all_classes=classes,
        departments=departments,
        selected_dept=selected_dept,
        selected_status=selected_status,
        teachers=teachers,
        students=students,
        card_type=card_type,
        selected_class_id=int(selected_class_id) if selected_class_id and selected_class_id.isdigit() else None,
        search_query=search_query,
        today=date.today()
    )


@main_bp.route('/admin/suspend_id_card', methods=['POST'])
@login_required
@admin_required
def admin_suspend_id_card():
    target_type = request.form.get('target_type', 'STUDENT').upper()
    target_id = request.form.get('target_id')
    reason = request.form.get('reason', '').strip()
    custom_reason = request.form.get('custom_reason', '').strip()

    if not target_id or not str(target_id).isdigit():
        flash("Invalid target ID specified for suspension.", "danger")
        return redirect(url_for('main.admin_id_cards'))

    from models import SuspensionAudit, ClassAnnouncement
    from datetime import datetime, timedelta
    now = datetime.utcnow()

    detention_days_raw = request.form.get('detention_days', '').strip()
    detention_days = int(detention_days_raw) if (detention_days_raw and detention_days_raw.isdigit() and int(detention_days_raw) > 0) else (2 if reason == 'Detained' else None)
    suspended_until = (now + timedelta(days=detention_days)) if (reason == 'Detained' and detention_days) else None

    if reason == 'Detained' and detention_days:
        full_reason = f"Detained for {detention_days} Days (Auto-releases on {suspended_until.strftime('%d %b %Y, %I:%M %p')})"
        if custom_reason:
            full_reason += f" - {custom_reason}"
    else:
        full_reason = custom_reason if (reason == 'Other' or not reason) else f"{reason}: {custom_reason}" if custom_reason else reason

    if not full_reason:
        full_reason = "Administrative suspension of ID Card and Campus Access."

    if target_type == 'STUDENT':
        student = Student.query.get_or_404(int(target_id))
        student.is_suspended = True
        student.id_card_status = 'Suspended'
        student.suspension_reason = reason or 'Administrative Issue'
        student.custom_suspension_reason = full_reason
        student.suspended_at = now
        student.suspended_by_user_id = current_user.id
        student.suspended_by_role = 'admin'
        student.suspended_by_name = current_user.name or 'Administrator'
        student.detention_days = detention_days
        student.suspended_until = suspended_until

        # Record in audit log
        audit = SuspensionAudit(
            target_type='STUDENT',
            student_id=student.id,
            action='SUSPENDED',
            reason=reason or 'Administrative Issue',
            custom_reason=full_reason,
            performed_by_user_id=current_user.id,
            performed_by_role='admin',
            performed_by_name=current_user.name or 'Administrator',
            created_at=now
        )
        db.session.add(audit)

        # Send official notification
        notice_detail = f"\n\nNote: Your detention will automatically expire and restore on {suspended_until.strftime('%d %b %Y, %I:%M %p')}." if suspended_until else "\n\nPlease visit the Admin Office or submit a suspension removal request via your portal."
        ann = ClassAnnouncement(
            title="🔒 Your ID Card Has Been Suspended" if reason != 'Detained' else "⚠️ ID Card Detained by Administration",
            content=f"Dear {student.name}, your institutional ID card and campus access have been {'DETAINED' if reason == 'Detained' else 'SUSPENDED'} by Administration.\n\nReason: {full_reason}\n\nYour attendance permissions and campus entry are blocked while active.{notice_detail}",
            target_role='STUDENTS',
            class_id=student.class_id,
            admin_id=current_user.id,
            posted_by_role='admin',
            created_at=now
        )
        db.session.add(ann)
        db.session.commit()
        status_msg = f"DETAINED for {detention_days} days (Auto-releases on {suspended_until.strftime('%d %b %Y')})" if suspended_until else "SUSPENDED"
        flash(f"✓ ID Card for student {student.name} ({student.roll_no}) has been {status_msg}. Campus access & attendance permissions are blocked.", "warning")
        return redirect(url_for('main.admin_id_cards', type='students', class_id=student.class_id or ''))

    elif target_type == 'TEACHER':
        teacher = Teacher.query.get_or_404(int(target_id))
        teacher.is_suspended = True
        teacher.id_card_status = 'Suspended'
        teacher.suspension_reason = reason or 'Administrative Issue'
        teacher.custom_suspension_reason = full_reason
        teacher.suspended_at = now
        teacher.suspended_by_user_id = current_user.id
        teacher.suspended_by_role = 'admin'
        teacher.suspended_by_name = current_user.name or 'Administrator'

        # Record in audit log
        audit = SuspensionAudit(
            target_type='TEACHER',
            teacher_id=teacher.id,
            action='SUSPENDED',
            reason=reason or 'Administrative Issue',
            custom_reason=full_reason,
            performed_by_user_id=current_user.id,
            performed_by_role='admin',
            performed_by_name=current_user.name or 'Administrator',
            created_at=now
        )
        db.session.add(audit)

        # Send official notification
        ann = ClassAnnouncement(
            title="🔒 Faculty ID Card Suspended",
            content=f"Faculty ID card for {teacher.name} has been SUSPENDED.\n\nReason: {full_reason}\n\nCampus biometric check-in & attendance recording permissions are blocked.",
            target_role='TEACHERS',
            admin_id=current_user.id,
            posted_by_role='admin',
            created_at=now
        )
        db.session.add(ann)
        db.session.commit()
        flash(f"✓ Faculty ID Card for {teacher.name} has been SUSPENDED.", "warning")
        return redirect(url_for('main.admin_id_cards', type='teachers'))

    flash("Invalid target type.", "danger")
    return redirect(url_for('main.admin_id_cards'))


@main_bp.route('/admin/restore_id_card', methods=['POST'])
@login_required
@admin_required
def admin_restore_id_card():
    target_type = request.form.get('target_type', 'STUDENT').upper()
    target_id = request.form.get('target_id')
    restore_note = request.form.get('restore_note', '').strip() or "Suspension lifted by Administrator."

    if not target_id or not str(target_id).isdigit():
        flash("Invalid target ID.", "danger")
        return redirect(url_for('main.admin_id_cards'))

    from models import SuspensionAudit, ClassAnnouncement
    now = datetime.utcnow()

    if target_type == 'STUDENT':
        student = Student.query.get_or_404(int(target_id))
        student.is_suspended = False
        student.id_card_status = 'Active'
        student.suspension_reason = None
        student.custom_suspension_reason = None
        student.suspended_at = None
        student.detention_days = None
        student.suspended_until = None
        student.suspended_by_user_id = None
        student.suspended_by_role = None
        student.suspended_by_name = None

        audit = SuspensionAudit(
            target_type='STUDENT',
            student_id=student.id,
            action='RESTORED',
            reason='Suspension Lifted',
            custom_reason=restore_note,
            performed_by_user_id=current_user.id,
            performed_by_role='admin',
            performed_by_name=current_user.name or 'Administrator',
            created_at=now
        )
        db.session.add(audit)

        ann = ClassAnnouncement(
            title="✓ ID Card Suspension Lifted & Active",
            content=f"Dear {student.name}, your ID card suspension has been revoked by Administration.\n\nYour campus access and attendance eligibility are fully restored.",
            target_role='STUDENTS',
            class_id=student.class_id,
            admin_id=current_user.id,
            posted_by_role='admin',
            created_at=now
        )
        db.session.add(ann)
        db.session.commit()
        flash(f"✓ ID Card for student {student.name} has been RESTORED to Active status! Campus access & attendance permissions are re-enabled.", "success")
        return redirect(url_for('main.admin_id_cards', type='students', class_id=student.class_id or ''))

    elif target_type == 'TEACHER':
        teacher = Teacher.query.get_or_404(int(target_id))
        teacher.is_suspended = False
        teacher.id_card_status = 'Active'
        teacher.suspension_reason = None
        teacher.custom_suspension_reason = None
        teacher.suspended_at = None

        audit = SuspensionAudit(
            target_type='TEACHER',
            teacher_id=teacher.id,
            action='RESTORED',
            reason='Suspension Lifted',
            custom_reason=restore_note,
            performed_by_user_id=current_user.id,
            performed_by_role='admin',
            performed_by_name=current_user.name or 'Administrator',
            created_at=now
        )
        db.session.add(audit)

        ann = ClassAnnouncement(
            title="✓ Faculty ID Card Suspension Revoked",
            content=f"Faculty ID card for {teacher.name} has been restored to Active status by Administration. Biometric check-in is now enabled.",
            target_role='TEACHERS',
            admin_id=current_user.id,
            posted_by_role='admin',
            created_at=now
        )
        db.session.add(ann)
        db.session.commit()
        flash(f"✓ Faculty ID Card for {teacher.name} has been RESTORED to Active status.", "success")
        return redirect(url_for('main.admin_id_cards', type='teachers'))

    flash("Invalid target type.", "danger")
    return redirect(url_for('main.admin_id_cards'))


@main_bp.route('/admin/suspension_requests')
@login_required
@admin_required
def admin_suspension_requests():
    from models import SuspensionRemovalRequest, SuspensionAudit
    requests_list = SuspensionRemovalRequest.query.order_by(SuspensionRemovalRequest.created_at.desc()).all()
    audit_logs = SuspensionAudit.query.order_by(SuspensionAudit.created_at.desc()).limit(100).all()
    active_suspended_students = Student.query.filter_by(is_suspended=True).all()
    active_suspended_teachers = Teacher.query.filter_by(is_suspended=True).all()

    return render_template(
        'admin_suspension_requests.html',
        requests_list=requests_list,
        audit_logs=audit_logs,
        active_suspended_students=active_suspended_students,
        active_suspended_teachers=active_suspended_teachers,
        today=date.today()
    )


@main_bp.route('/admin/review_suspension_request/<int:req_id>', methods=['POST'])
@login_required
@admin_required
def review_suspension_request(req_id):
    from models import SuspensionRemovalRequest, SuspensionAudit, ClassAnnouncement
    req_obj = SuspensionRemovalRequest.query.get_or_404(req_id)
    action = request.form.get('action', 'APPROVE').upper() # 'APPROVE', 'REJECT', 'CONTINUE'
    admin_notes = request.form.get('admin_notes', '').strip()
    now = datetime.utcnow()

    if action == 'APPROVE':
        req_obj.status = 'Approved'
        req_obj.admin_notes = admin_notes or "Suspension removal request approved by Admin."
        req_obj.reviewed_by_user_id = current_user.id
        req_obj.reviewed_at = now

        if req_obj.target_type == 'STUDENT' and req_obj.student:
            req_obj.student.is_suspended = False
            req_obj.student.id_card_status = 'Active'
            req_obj.student.suspension_reason = None
            req_obj.student.custom_suspension_reason = None
            req_obj.student.suspended_at = None
            req_obj.student.detention_days = None
            req_obj.student.suspended_until = None
            req_obj.student.suspended_by_user_id = None
            req_obj.student.suspended_by_role = None
            req_obj.student.suspended_by_name = None

            audit = SuspensionAudit(
                target_type='STUDENT',
                student_id=req_obj.student.id,
                action='RESTORED',
                reason='Removal Request Approved',
                custom_reason=admin_notes,
                performed_by_user_id=current_user.id,
                performed_by_role='admin',
                performed_by_name=current_user.name or 'Administrator',
                created_at=now
            )
            db.session.add(audit)

            ann = ClassAnnouncement(
                title="✓ Suspension Removal Request Approved",
                content=f"Dear {req_obj.student.name}, your request to lift ID card suspension has been APPROVED.\n\nAdmin Remarks: {admin_notes or 'All campus permissions and attendance recording eligibility restored.'}",
                target_role='STUDENTS',
                class_id=req_obj.student.class_id,
                admin_id=current_user.id,
                posted_by_role='admin',
                created_at=now
            )
            db.session.add(ann)

        elif req_obj.target_type == 'TEACHER' and req_obj.teacher:
            req_obj.teacher.is_suspended = False
            req_obj.teacher.id_card_status = 'Active'
            req_obj.teacher.suspension_reason = None
            req_obj.teacher.custom_suspension_reason = None
            req_obj.teacher.suspended_at = None

            audit = SuspensionAudit(
                target_type='TEACHER',
                teacher_id=req_obj.teacher.id,
                action='RESTORED',
                reason='Removal Request Approved',
                custom_reason=admin_notes,
                performed_by_user_id=current_user.id,
                performed_by_role='admin',
                performed_by_name=current_user.name or 'Administrator',
                created_at=now
            )
            db.session.add(audit)

        db.session.commit()
        flash("✓ Suspension removal request APPROVED! ID card status restored to Active and permissions re-enabled.", "success")

    elif action == 'REJECT':
        req_obj.status = 'Rejected'
        req_obj.admin_notes = admin_notes or "Removal request rejected after administrative review."
        req_obj.reviewed_by_user_id = current_user.id
        req_obj.reviewed_at = now

        name = req_obj.student.name if req_obj.student else (req_obj.teacher.name if req_obj.teacher else "User")
        cls_id = req_obj.student.class_id if req_obj.student else None
        target_r = 'STUDENTS' if req_obj.target_type == 'STUDENT' else 'TEACHERS'

        ann = ClassAnnouncement(
            title="⚠️ Suspension Removal Request Rejected",
            content=f"Dear {name}, your suspension removal request has been REJECTED.\n\nReason/Remarks: {admin_notes or 'Suspension remains active. Please contact the Admin Office in person.'}",
            target_role=target_r,
            class_id=cls_id,
            admin_id=current_user.id,
            posted_by_role='admin',
            created_at=now
        )
        db.session.add(ann)
        db.session.commit()
        flash("Suspension removal request has been REJECTED. ID card remains suspended.", "warning")

    return redirect(url_for('main.admin_suspension_requests'))

@main_bp.route('/admin/university_details', methods=['GET', 'POST'])
@login_required
@admin_required
def university_details():
    settings = UniversitySettings.get_settings()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        short_name = request.form.get('short_name', '').strip()
        slogan = request.form.get('slogan', '').strip()
        president_name = request.form.get('president_name', '').strip()
        dean_name = request.form.get('dean_name', '').strip()
        registrar_name = request.form.get('registrar_name', '').strip()
        address = request.form.get('address', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        website = request.form.get('website', '').strip()
        accreditation = request.form.get('accreditation', '').strip()
        established_year = request.form.get('established_year', '').strip()

        if name:
            settings.name = name
        if short_name:
            settings.short_name = short_name
        settings.slogan = slogan or settings.slogan
        settings.president_name = president_name or settings.president_name
        settings.dean_name = dean_name or settings.dean_name
        settings.registrar_name = registrar_name or settings.registrar_name
        settings.address = address or settings.address
        settings.phone = phone or settings.phone
        settings.email = email or settings.email
        settings.website = website or settings.website
        settings.accreditation = accreditation or settings.accreditation
        settings.established_year = established_year or settings.established_year

        # Handle Logo File Upload
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            u_dir = os.path.join(os.getcwd(), 'uploads', 'university')
            os.makedirs(u_dir, exist_ok=True)
            ext = os.path.splitext(secure_filename(logo_file.filename))[1] or '.png'
            logo_fn = f"univ_logo_{int(datetime.utcnow().timestamp())}{ext}"
            logo_path = os.path.join(u_dir, logo_fn)
            file_bytes = logo_file.read()
            with open(logo_path, 'wb') as f:
                f.write(file_bytes)
            settings.logo_filename = logo_fn
            settings.logo_data = base64.b64encode(file_bytes).decode('utf-8')

        # Handle College Name Image / Wordmark Typography Banner Upload
        name_image_file = request.files.get('name_image')
        if name_image_file and name_image_file.filename:
            u_dir = os.path.join(os.getcwd(), 'uploads', 'university')
            os.makedirs(u_dir, exist_ok=True)
            ext = os.path.splitext(secure_filename(name_image_file.filename))[1] or '.png'
            name_img_fn = f"univ_name_{int(datetime.utcnow().timestamp())}{ext}"
            name_img_path = os.path.join(u_dir, name_img_fn)
            file_bytes = name_image_file.read()
            with open(name_img_path, 'wb') as f:
                f.write(file_bytes)
            settings.name_image_filename = name_img_fn
            settings.name_image_data = base64.b64encode(file_bytes).decode('utf-8')

        if request.form.get('remove_name_image') == '1':
            settings.name_image_filename = None
            settings.name_image_data = None

        # Header Display Mode
        display_mode = request.form.get('header_display_mode', 'BOTH')
        if display_mode in ('TEXT', 'IMAGE', 'BOTH'):
            settings.header_display_mode = display_mode

        # Handle Signature Stamp Upload
        sig_file = request.files.get('signature')
        if sig_file and sig_file.filename:
            u_dir = os.path.join(os.getcwd(), 'uploads', 'university')
            os.makedirs(u_dir, exist_ok=True)
            ext = os.path.splitext(secure_filename(sig_file.filename))[1] or '.png'
            sig_fn = f"univ_signature_{int(datetime.utcnow().timestamp())}{ext}"
            sig_path = os.path.join(u_dir, sig_fn)
            file_bytes = sig_file.read()
            with open(sig_path, 'wb') as f:
                f.write(file_bytes)
            settings.signature_filename = sig_fn
            settings.signature_data = base64.b64encode(file_bytes).decode('utf-8')

        db.session.commit()
        flash("✓ University institutional profile, college name wordmark & branding successfully updated! All portals, headers, and ID cards now reflect the updated details.", "success")
        return redirect(url_for('main.university_details'))

    return render_template('admin_university_details.html', settings=settings)


# ==============================================================================
# SECTION 17: ADMIN FEEDBACK, RATING & COMPLAINT MANAGEMENT DESK
# ==============================================================================

@main_bp.route('/admin/feedback_management')
@login_required
@admin_required
def feedback_management():
    """
    Centralized administrative hub for:
    1. Faculty Evaluation Analytics & Leaderboard (sorting, filtering by dept/class/subject)
    2. Student Reviews & 4-parameter scoring matrix
    3. Class-Level Faculty Complaints & Live Voting tracker
    4. Threshold-reached high priority review queue
    """
    active_tab = request.args.get('tab', 'rankings').strip()
    selected_dept = request.args.get('department', '').strip()
    selected_class_id = request.args.get('class_id', type=int)

    all_departments = Department.query.order_by(Department.name.asc()).all()
    all_classes = get_admin_classes()

    # Query teachers
    teacher_query = Teacher.query
    if selected_dept:
        teacher_query = teacher_query.filter_by(department=selected_dept)
    teachers = teacher_query.order_by(Teacher.name.asc()).all()

    # Build Faculty Analytics Matrix
    faculty_analytics = []
    for t in teachers:
        feedbacks = TeacherFeedback.query.filter_by(teacher_id=t.id).all()
        complaints = FacultyComplaint.query.filter_by(teacher_id=t.id).all()
        
        # If class filter is applied
        if selected_class_id:
            feedbacks = [f for f in feedbacks if f.class_id == selected_class_id]
            complaints = [c for c in complaints if c.class_id == selected_class_id]

        total_reviews = len(feedbacks)
        total_complaints = len(complaints)
        active_complaints = sum(1 for c in complaints if c.status in ['Voting in Progress', 'Threshold Reached', 'Under Review', 'Action Required'])
        threshold_complaints = sum(1 for c in complaints if c.status == 'Threshold Reached')

        avg_overall = round(sum(f.overall_rating for f in feedbacks) / total_reviews, 2) if total_reviews > 0 else 0.0
        avg_quality = round(sum(f.teaching_quality for f in feedbacks) / total_reviews, 2) if total_reviews > 0 else 0.0
        avg_knowledge = round(sum(f.subject_knowledge for f in feedbacks) / total_reviews, 2) if total_reviews > 0 else 0.0
        avg_comm = round(sum(f.communication_style for f in feedbacks) / total_reviews, 2) if total_reviews > 0 else 0.0
        avg_support = round(sum(f.student_support for f in feedbacks) / total_reviews, 2) if total_reviews > 0 else 0.0

        if avg_overall >= 4.5:
            perf_status = 'Excellent'
            badge_class = 'success'
        elif avg_overall >= 3.8:
            perf_status = 'Good'
            badge_class = 'primary'
        elif avg_overall >= 3.0:
            perf_status = 'Satisfactory'
            badge_class = 'info'
        elif total_reviews > 0:
            perf_status = 'Needs Review'
            badge_class = 'danger'
        else:
            perf_status = 'No Ratings Yet'
            badge_class = 'secondary'

        faculty_analytics.append({
            'teacher': t,
            'total_reviews': total_reviews,
            'total_complaints': total_complaints,
            'active_complaints': active_complaints,
            'threshold_complaints': threshold_complaints,
            'avg_overall': avg_overall,
            'avg_quality': avg_quality,
            'avg_knowledge': avg_knowledge,
            'avg_comm': avg_comm,
            'avg_support': avg_support,
            'perf_status': perf_status,
            'badge_class': badge_class,
            'feedbacks': feedbacks,
            'complaints': complaints
        })

    # Sort faculty by rating descending (high performers first)
    faculty_analytics.sort(key=lambda x: (x['avg_overall'], x['total_reviews']), reverse=True)

    # Query all complaints
    complaint_query = FacultyComplaint.query
    if selected_dept:
        complaint_query = complaint_query.join(Teacher).filter(Teacher.department == selected_dept)
    if selected_class_id:
        complaint_query = complaint_query.filter(FacultyComplaint.class_id == selected_class_id)
    
    all_complaints = complaint_query.order_by(
        FacultyComplaint.status.desc(), # 'Threshold Reached' on top
        FacultyComplaint.created_at.desc()
    ).all()

    # Query all reviews
    feedback_query = TeacherFeedback.query
    if selected_class_id:
        feedback_query = feedback_query.filter_by(class_id=selected_class_id)
    all_feedbacks = feedback_query.order_by(TeacherFeedback.updated_at.desc()).limit(200).all()

    # Threshold alerts count
    threshold_alerts_count = sum(1 for c in all_complaints if c.status == 'Threshold Reached')

    # All active teachers for replacement dropdown
    all_active_teachers = Teacher.query.filter_by(status='Approved').order_by(Teacher.name.asc()).all()

    # Pre-calculate eligible / free replacement teachers for each complaint
    complaint_free_teachers = {}
    for c in all_complaints:
        if c.is_replacement_requested and c.class_id:
            # Find the timetable slots associated with the current teacher in that class
            slots_query = Timetable.query.filter_by(class_id=c.class_id, teacher_id=c.teacher_id)
            if c.subject_id:
                slots_query = slots_query.filter_by(subject_id=c.subject_id)
            current_slots = slots_query.all()

            free_list = []
            busy_list = []
            for candidate in all_active_teachers:
                if c.teacher_id and candidate.id == c.teacher_id:
                    continue

                # Check if candidate has schedule conflict in any of current_slots
                has_conflict = False
                conflict_details = []
                for s in current_slots:
                    clash = Timetable.query.filter(
                        Timetable.teacher_id == candidate.id,
                        Timetable.day_of_week == s.day_of_week,
                        Timetable.period_no == s.period_no,
                        Timetable.id != s.id
                    ).first()
                    if clash:
                        has_conflict = True
                        conflict_details.append(f"{clash.day_of_week} P{clash.period_no} ({clash.class_assigned.name if clash.class_assigned else 'Class'})")

                if not has_conflict:
                    free_list.append({
                        'teacher': candidate,
                        'is_free': True,
                        'dept_match': (candidate.department == c.class_assigned.department) if c.class_assigned and candidate.department else False
                    })
                else:
                    busy_list.append({
                        'teacher': candidate,
                        'is_free': False,
                        'conflict_summary': ', '.join(conflict_details[:2])
                    })

            # Sort free candidates by department match first, then name
            free_list.sort(key=lambda x: (not x['dept_match'], x['teacher'].name))
            complaint_free_teachers[c.id] = {
                'free': free_list,
                'busy': busy_list,
                'has_free': len(free_list) > 0
            }

    return render_template(
        'admin_feedback_management.html',
        faculty_analytics=faculty_analytics,
        all_complaints=all_complaints,
        all_feedbacks=all_feedbacks,
        all_departments=all_departments,
        all_classes=all_classes,
        all_active_teachers=all_active_teachers,
        complaint_free_teachers=complaint_free_teachers,
        selected_dept=selected_dept,
        selected_class_id=selected_class_id,
        threshold_alerts_count=threshold_alerts_count,
        active_tab=active_tab
    )


@main_bp.route('/admin/update_complaint_status/<int:complaint_id>', methods=['POST'])
@login_required
@admin_required
def update_complaint_status(complaint_id):
    """Administrative action on a class complaint (e.g. Under Review, Action Required, Resolved, Rejected, Closed)."""
    complaint = FacultyComplaint.query.get_or_404(complaint_id)
    new_status = request.form.get('status', '').strip()
    admin_notes = request.form.get('admin_notes', '').strip()

    valid_statuses = ['Voting in Progress', 'Threshold Reached', 'Under Review', 'Action Required', 'Resolved', 'Rejected', 'Closed']
    if new_status not in valid_statuses:
        flash("Invalid status choice.", "warning")
        return redirect(url_for('main.feedback_management', tab='complaints'))

    try:
        complaint.status = new_status
        if admin_notes:
            complaint.admin_notes = admin_notes
        complaint.reviewed_by_user_id = current_user.id
        complaint.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f"✓ Complaint #{complaint.id} status updated to '{new_status}' successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating complaint: {e}", "danger")

    return redirect(url_for('main.feedback_management', tab='complaints'))


@main_bp.route('/admin/replace_faculty_from_complaint/<int:complaint_id>', methods=['POST'])
@login_required
@admin_required
def replace_faculty_from_complaint(complaint_id):
    """
    Direct Faculty Replacement Action:
    Replaces current faculty with selected replacement faculty after checking timetable conflicts:
    1. Validates no schedule / period clashes exist for the new faculty across class timetable slots.
    2. Updates Subject teacher allocation (`subjects.teacher_id`) for that class.
    3. Reallocates weekly Timetable entries (`timetables.teacher_id`).
    4. Updates teacher-class assignments (`teacher_assignments`).
    5. Updates complaint status to 'Resolved' with replacement audit notes.
    """
    complaint = FacultyComplaint.query.get_or_404(complaint_id)
    new_teacher_id = request.form.get('new_teacher_id', type=int)

    if not new_teacher_id:
        flash("Please select a valid replacement faculty.", "warning")
        return redirect(url_for('main.feedback_management', tab='complaints'))

    old_teacher = complaint.teacher
    new_teacher = Teacher.query.get_or_404(new_teacher_id)
    target_class = complaint.class_assigned
    target_subject = complaint.subject_assigned

    if not target_class:
        flash("No class linked with this complaint.", "danger")
        return redirect(url_for('main.feedback_management', tab='complaints'))

    try:
        # Find all timetable slots for this class & teacher (or subject)
        timetable_query = Timetable.query.filter_by(class_id=target_class.id, teacher_id=old_teacher.id)
        if target_subject:
            timetable_query = timetable_query.filter_by(subject_id=target_subject.id)
        current_slots = timetable_query.all()

        # Check for schedule conflicts on the new faculty's timetable
        clashes = []
        for slot in current_slots:
            clash_slot = Timetable.query.filter(
                Timetable.teacher_id == new_teacher.id,
                Timetable.day_of_week == slot.day_of_week,
                Timetable.period_no == slot.period_no,
                Timetable.id != slot.id
            ).first()
            if clash_slot:
                clashes.append(f"{slot.day_of_week} Period {slot.period_no} (Busy with {clash_slot.class_assigned.name if clash_slot.class_assigned else 'Class'})")

        if clashes:
            clash_str = "; ".join(clashes[:3])
            flash(f"⚠️ Cannot replace: {new_teacher.name} has a timetable conflict on {clash_str}. Complaint remains in progress / pending.", "danger")
            complaint.status = 'Action Required'
            complaint.admin_notes = f"Attempted replacement with {new_teacher.name} failed due to timetable clashes: {clash_str}."
            db.session.commit()
            return redirect(url_for('main.feedback_management', tab='complaints'))

        # 1. Update Subject teacher allocation
        updated_subjects = 0
        if target_subject:
            target_subject.teacher_id = new_teacher.id
            updated_subjects += 1
        else:
            subs = Subject.query.filter_by(class_id=target_class.id, teacher_id=old_teacher.id).all()
            for s in subs:
                s.teacher_id = new_teacher.id
                updated_subjects += 1

        # 2. Update Teacher Assignment
        if target_subject:
            assign = TeacherAssignment.query.filter_by(
                class_id=target_class.id,
                subject_id=target_subject.id,
                teacher_id=old_teacher.id
            ).first()
            if assign:
                assign.teacher_id = new_teacher.id
            else:
                db.session.add(TeacherAssignment(
                    class_id=target_class.id,
                    subject_id=target_subject.id,
                    teacher_id=new_teacher.id
                ))

        # 3. Update Timetable entries
        updated_slots = 0
        for slot in current_slots:
            slot.teacher_id = new_teacher.id
            updated_slots += 1

        # 4. Mark Complaint as Resolved with replacement audit record
        complaint.status = 'Resolved'
        complaint.admin_notes = f"✓ Faculty Replaced: {old_teacher.name} replaced by {new_teacher.name} for {target_class.name} ({target_subject.name if target_subject else 'Subject'}). Timetables ({updated_slots} slots) updated without conflict."
        complaint.reviewed_by_user_id = current_user.id
        complaint.updated_at = datetime.utcnow()

        db.session.commit()
        flash(f"✓ Success! Faculty {old_teacher.name} successfully replaced by {new_teacher.name} in {target_class.name}. Timetables ({updated_slots} slots) and subjects updated seamlessly!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error replacing faculty: {e}", "danger")

    return redirect(url_for('main.feedback_management', tab='complaints'))


@main_bp.app_context_processor
def inject_pending_approvals():
    if current_user.is_authenticated and current_user.role == 'admin':
        try:
            class_ids = [c.id for c in get_admin_classes()]
            student_req_count = StudentEditRequest.query.join(Student).filter(
                Student.class_id.in_(class_ids),
                StudentEditRequest.status == 'Pending'
            ).count()
            teacher_pending_count = Teacher.query.filter_by(status='Pending').count()
            teacher_edit_req_count = TeacherEditRequest.query.filter_by(status='Pending').count()
            threshold_complaint_count = FacultyComplaint.query.filter_by(status='Threshold Reached').count()

            total_pending = student_req_count + teacher_pending_count + teacher_edit_req_count
            unclassified_count = Student.query.filter(Student.class_id == None).count()
            return {
                'pending_approvals_count': total_pending,
                'unclassified_students_count': unclassified_count,
                'admin_threshold_complaints_count': threshold_complaint_count
            }
        except Exception:
            return {'pending_approvals_count': 0, 'unclassified_students_count': 0, 'admin_threshold_complaints_count': 0}
    return {'pending_approvals_count': 0, 'unclassified_students_count': 0, 'admin_threshold_complaints_count': 0}