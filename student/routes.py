# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - STUDENT PORTAL ROUTING MODULE
# ==============================================================================
# Description: Handles student dashboard metrics, live daily lecture schedules,
#              attendance percentage graphs, discrepancy requests, announcements,
#              biometric profile edit requests, and printable attendance reports.
# ==============================================================================

import os
try:
    import face_recognition
except ImportError:
    face_recognition = None
import numpy as np
from datetime import date, datetime, timedelta
from flask import render_template, redirect, url_for, flash, request, Blueprint, current_app, jsonify
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from functools import wraps

from extensions import db, get_current_date
from models import (
    User, Student, Class, Subject, Attendance, StudentEditRequest, ClassAnnouncement, StudentDismissedNotice,
    StudentReadNotice, Timetable, DailySchedule, AttendanceSession, AttendanceRecord, AttendanceDiscrepancyRequest, Holiday, Department
)
from schedule_service import generate_daily_schedule, calculate_student_attendance
from auth.routes import save_base64_image

student_bp = Blueprint('student', __name__)

FACES_FOLDER = os.path.join('temp_uploads', 'faces')

# ==============================================================================
# SECTION 1: ROLE-BASED ACCESS CONTROL DECORATOR
# ==============================================================================
def student_required(f):
    """Restricts route access exclusively to authenticated student accounts."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'student':
            flash("Student access is required to view this page.", "danger")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

# ==============================================================================
# SECTION 2: STUDENT DASHBOARD & ANALYTICS
# ==============================================================================

@student_bp.route('/student/dashboard')
@login_required
@student_required
def dashboard():
    student = current_user.student_profile
    if not student:
        flash("Student profile not found. Please contact administration.", "danger")
        return redirect(url_for('auth.logout'))

    today = get_current_date()
    # Trigger daily schedule generator for today
    generate_daily_schedule(today)

    subjects = Subject.query.filter_by(class_id=student.class_id).all() if student.class_id else []
    
    subject_names = []
    attendance_percentages = []
    subject_stats = {}

    overall_stats = calculate_student_attendance(student.id)
    total_classes_conducted = overall_stats['completed_sessions']
    total_classes_attended = overall_stats['attended']
    missed_classes = overall_stats['missed']
    overall_attendance_pct = overall_stats['percentage']

    for subject in subjects:
        sub_stats = calculate_student_attendance(student.id, subject_id=subject.id)
        pct = sub_stats['percentage']
        subject_names.append(subject.name)
        attendance_percentages.append(pct)
        
        subject_stats[subject.name] = {
            'present': sub_stats['attended'],
            'total': sub_stats['completed_sessions'],
            'missed': sub_stats['missed'],
            'percentage': pct,
            'teacher': subject.teacher.name if subject.teacher else "No Teacher"
        }

    # 1. Daily Stats (Today COMPLETED Sessions for active class slots)
    today_sessions = AttendanceSession.query.filter(
        AttendanceSession.class_id == student.class_id,
        AttendanceSession.date == today,
        AttendanceSession.status == 'COMPLETED',
        AttendanceSession.timetable_id != None
    ).all() if student.class_id else []
    today_s_ids = [s.id for s in today_sessions]
    daily_attended = AttendanceRecord.query.filter(
        AttendanceRecord.session_id.in_(today_s_ids),
        AttendanceRecord.student_id == student.id,
        AttendanceRecord.status == 'PRESENT'
    ).count() if today_s_ids else 0

    today_class_slots_count = Timetable.query.filter_by(class_id=student.class_id, day_of_week=today.strftime('%A'), slot_type='CLASS').count() if student.class_id else 6
    daily_total = min(len(today_sessions), today_class_slots_count or 6) if today_sessions else (today_class_slots_count or 6)
    daily_pct = round((daily_attended / daily_total * 100), 2) if daily_total > 0 else 0.0

    # 2. Weekly Stats (This Week COMPLETED Sessions)
    start_of_week = today - timedelta(days=today.weekday())
    weekly_sessions = AttendanceSession.query.filter(
        AttendanceSession.class_id == student.class_id,
        AttendanceSession.date >= start_of_week,
        AttendanceSession.status == 'COMPLETED'
    ).all() if student.class_id else []
    weekly_s_ids = [s.id for s in weekly_sessions]
    weekly_attended = AttendanceRecord.query.filter(
        AttendanceRecord.session_id.in_(weekly_s_ids),
        AttendanceRecord.student_id == student.id,
        AttendanceRecord.status == 'PRESENT'
    ).count() if weekly_s_ids else 0
    weekly_total = len(weekly_sessions)
    weekly_pct = round((weekly_attended / weekly_total * 100), 2) if weekly_total > 0 else 0.0

    # 3. Monthly Stats (This Month COMPLETED Sessions)
    start_of_month = date(today.year, today.month, 1)
    monthly_sessions = AttendanceSession.query.filter(
        AttendanceSession.class_id == student.class_id,
        AttendanceSession.date >= start_of_month,
        AttendanceSession.status == 'COMPLETED'
    ).all() if student.class_id else []
    monthly_s_ids = [s.id for s in monthly_sessions]
    monthly_attended = AttendanceRecord.query.filter(
        AttendanceRecord.session_id.in_(monthly_s_ids),
        AttendanceRecord.student_id == student.id,
        AttendanceRecord.status == 'PRESENT'
    ).count() if monthly_s_ids else 0
    monthly_total = len(monthly_sessions)
    monthly_pct = round((monthly_attended / monthly_total * 100), 2) if monthly_total > 0 else 0.0

    # 4. Build Full Weekly Timetable Schedule (Monday to Saturday)
    days_map = {
        'Monday': start_of_week,
        'Tuesday': start_of_week + timedelta(days=1),
        'Wednesday': start_of_week + timedelta(days=2),
        'Thursday': start_of_week + timedelta(days=3),
        'Friday': start_of_week + timedelta(days=4),
        'Saturday': start_of_week + timedelta(days=5),
    }

    all_weekly_slots = Timetable.query.filter_by(
        class_id=student.class_id
    ).order_by(Timetable.period_no, Timetable.start_time).all() if student.class_id else []

    full_student_timetable = []
    daily_timetable_status = []

    for d_name in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']:
        d_date = days_map[d_name]
        d_slots = [s for s in all_weekly_slots if s.day_of_week == d_name]
        
        for tt in d_slots:
            if tt.slot_type != 'CLASS':
                continue

            sess = AttendanceSession.query.filter(
                ((AttendanceSession.timetable_id == tt.id) & (AttendanceSession.date == d_date)) |
                ((AttendanceSession.subject_id == tt.subject_id) & (AttendanceSession.class_id == student.class_id) & (AttendanceSession.date == d_date) & (AttendanceSession.start_time == tt.start_time))
            ).first()

            rec = None
            if sess:
                rec = AttendanceRecord.query.filter_by(session_id=sess.id, student_id=student.id).first()

            # Check if this slot was cancelled or proxy assigned
            ds = DailySchedule.query.filter_by(timetable_id=tt.id, date=d_date).first()
            is_cancelled = False
            cancellation_reason = ""
            is_proxy = False
            proxy_teacher_name = None

            if ds and (ds.is_cancelled or ds.resolved_status == 'CANCELLED'):
                status_label = 'Cancelled'
                status_class = 'danger'
                status_icon = 'fa-ban'
                marked_time = 'Class Cancelled'
                is_cancelled = True
                cancellation_reason = ds.cancellation_reason or 'Faculty Unavailable (Cancelled by Admin)'
            elif ds and (ds.is_proxy or ds.resolved_status == 'SUBSTITUTE_ASSIGNED') and ds.substitute_teacher_id:
                is_proxy = True
                proxy_teacher_name = ds.substitute_teacher.name if ds.substitute_teacher else 'Substitute'
                if not rec and d_date == today:
                    status_label = 'Proxy Assigned'
                    status_class = 'info'
                    status_icon = 'fa-user-shield'
                    marked_time = f'Proxy: Prof. {proxy_teacher_name}'

            if not is_cancelled:
                if rec:
                    if rec.status == 'PRESENT':
                        status_label = 'Present'
                        status_class = 'success'
                        status_icon = 'fa-circle-check'
                        marked_time = (rec.marked_at + timedelta(hours=5, minutes=30)).strftime("%I:%M %p") if rec.marked_at else 'Marked'
                    else:
                        status_label = 'Absent'
                        status_class = 'danger'
                        status_icon = 'fa-circle-xmark'
                        marked_time = 'Absent'
                elif sess and sess.status == 'COMPLETED':
                    status_label = 'Absent'
                    status_class = 'danger'
                    status_icon = 'fa-circle-xmark'
                    marked_time = 'Absent'
                elif d_date < today:
                    status_label = 'Past Class'
                    status_class = 'secondary'
                    status_icon = 'fa-clock-rotate-left'
                    marked_time = 'Not Conducted'
                elif d_date == today:
                    if not is_proxy:
                        status_label = 'Scheduled'
                        status_class = 'warning'
                        status_icon = 'fa-clock'
                        marked_time = 'Upcoming Today'
                else:
                    status_label = 'Upcoming'
                    status_class = 'info'
                    status_icon = 'fa-calendar'
                    marked_time = 'Upcoming'

            item_dict = {
                'session_id': sess.id if sess else None,
                'slot': tt,
                'period': tt.period_no or 1,
                'subject': tt.subject_assigned.name if tt.subject_assigned else (tt.slot_type or 'N/A'),
                'teacher': tt.teacher_assigned.name if tt.teacher_assigned else 'Faculty Unassigned',
                'is_proxy': is_proxy,
                'proxy_teacher_name': proxy_teacher_name,
                'is_cancelled': is_cancelled,
                'cancellation_reason': cancellation_reason,
                'day': tt.day_of_week,
                'date_str': d_date.strftime('%b %d'),
                'date': d_date,
                'time_slot': f"{tt.start_time} - {tt.end_time}",
                'room_number': tt.room or 'Room N/A',
                'status_label': status_label,
                'status_class': status_class,
                'status_icon': status_icon,
                'marked_time': marked_time
            }
            full_student_timetable.append(item_dict)
            if d_date == today:
                daily_timetable_status.append(item_dict)

    today_cancelled_classes = [c for c in daily_timetable_status if c.get('is_cancelled')]

    # Subject breakdown schedule map
    timetable_schedule = []
    for sub in subjects:
        st = subject_stats.get(sub.name, {})
        timetable_schedule.append({
            'subject': sub.name,
            'teacher': sub.teacher.name if sub.teacher else "Faculty Unassigned",
            'day': 'Weekly',
            'time_slot': 'Regular Slot',
            'room_number': 'Classroom',
            'attended': st.get('present', 0),
            'total': st.get('total', 0),
            'pct': st.get('percentage', 0.0)
        })

    # Fetch student's attendance records history (Scoped to official timetable sessions only)
    raw_records = AttendanceRecord.query.join(AttendanceSession).filter(
        AttendanceRecord.student_id == student.id,
        AttendanceSession.timetable_id != None
    ).order_by(AttendanceRecord.marked_at.desc()).all()
    seen_slots = set()
    records = []
    for r in raw_records:
        sess = r.session
        key = (sess.date, sess.timetable_id or sess.subject_id, sess.start_time) if sess else r.id
        if key not in seen_slots:
            seen_slots.add(key)
            records.append(r)

    pending_request = StudentEditRequest.query.filter_by(student_id=student.id, status='Pending').first()
    class_notices = ClassAnnouncement.query.filter_by(class_id=student.class_id).order_by(ClassAnnouncement.created_at.desc()).all() if student.class_id else []

    return render_template(
        'student_dashboard.html',
        student=student,
        total_classes_conducted=total_classes_conducted,
        total_classes_attended=total_classes_attended,
        missed_classes=missed_classes,
        overall_attendance_pct=overall_attendance_pct,
        daily_stats={'attended': daily_attended, 'total': daily_total, 'pct': daily_pct},
        weekly_stats={'attended': weekly_attended, 'total': weekly_total, 'pct': weekly_pct},
        monthly_stats={'attended': monthly_attended, 'total': monthly_total, 'pct': monthly_pct},
        semester_stats={'attended': total_classes_attended, 'total': total_classes_conducted, 'pct': overall_attendance_pct},
        timetable_schedule=timetable_schedule,
        daily_timetable_status=daily_timetable_status,
        full_student_timetable=full_student_timetable,
        subject_names=subject_names,
        attendance_percentages=attendance_percentages,
        subject_stats=subject_stats,
        records=records,
        pending_request=pending_request,
        class_notices=class_notices,
        today_cancelled_classes=today_cancelled_classes,
        today=today.strftime('%Y-%m-%d')
    )

@student_bp.route('/student/edit_profile', methods=['GET', 'POST'])
@login_required
@student_required
def edit_profile():
    student = current_user.student_profile
    if not student:
        flash("Student profile not found.", "danger")
        return redirect(url_for('auth.logout'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        roll_no = request.form.get('roll_no', '').strip()
        enrollment_no = request.form.get('enrollment_no', '').strip()
        department = request.form.get('department', '').strip()
        mobile = request.form.get('mobile', '').strip()
        class_id = request.form.get('class_id')
        captured_image = request.form.get('captured_image')

        if not name or not roll_no or not enrollment_no or not class_id:
            flash("All profile fields (Name, Roll No, Enrollment No, Class) are required.", "warning")
            return redirect(url_for('student.edit_profile'))

        c_id = int(class_id)
        chosen_class = Class.query.get(c_id)
        if chosen_class and chosen_class.department:
            department = chosen_class.department

        new_filename = student.image_filename
        new_encoding_bytes = student.face_embedding

        if captured_image and captured_image.startswith('data:image'):
            try:
                fname, enc_bytes = save_base64_image(captured_image, prefix=f"pending_student_{student.id}_")
                if fname:
                    new_filename = fname
                    new_encoding_bytes = enc_bytes
            except Exception as e:
                flash(f"Error processing facial photo: {e}", "danger")
                return redirect(url_for('student.edit_profile'))

        # Check existing pending request
        pending_req = StudentEditRequest.query.filter_by(student_id=student.id, status='Pending').first()
        if pending_req:
            pending_req.new_name = name
            pending_req.new_roll_no = roll_no
            pending_req.new_enrollment_no = enrollment_no
            pending_req.new_department = department or student.department
            pending_req.new_mobile = mobile
            pending_req.new_class_id = c_id
            pending_req.new_image_filename = new_filename
            pending_req.new_face_encoding = new_encoding_bytes
            flash("Updated your pending profile edit request! Awaiting admin review.", "success")
        else:
            new_req = StudentEditRequest(
                student_id=student.id,
                new_name=name,
                new_roll_no=roll_no,
                new_enrollment_no=enrollment_no,
                new_department=department or student.department,
                new_mobile=mobile,
                new_class_id=c_id,
                new_image_filename=new_filename,
                new_face_encoding=new_encoding_bytes,
                status='Pending'
            )
            db.session.add(new_req)
            flash("Profile edit request submitted successfully! Awaiting admin approval.", "success")

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Error submitting edit request: {e}", "danger")

        return redirect(url_for('student.dashboard'))

    classes = Class.query.all()
    all_departments = Department.query.order_by(Department.name.asc()).all()
    pending_request = StudentEditRequest.query.filter_by(student_id=student.id, status='Pending').first()

    return render_template('student_profile_edit.html', student=student, classes=classes, all_departments=all_departments, pending_request=pending_request)

@student_bp.route('/student/report_discrepancy', methods=['POST'])
@login_required
@student_required
def report_discrepancy():
    student = current_user.student_profile
    if not student:
        flash("Student profile not found.", "danger")
        return redirect(url_for('student.dashboard'))

    session_id = request.form.get('session_id')
    reason = request.form.get('reason', '').strip()

    if not session_id or not str(session_id).isdigit() or not reason:
        flash("Session ID and valid reason are required to report a discrepancy.", "warning")
        return redirect(url_for('student.dashboard'))

    sess = AttendanceSession.query.get(int(session_id))
    if not sess:
        flash("Attendance session not found.", "danger")
        return redirect(url_for('student.dashboard'))

    existing_req = AttendanceDiscrepancyRequest.query.filter_by(
        student_id=student.id,
        session_id=sess.id,
        status='PENDING'
    ).first()

    if existing_req:
        flash("You already have a pending discrepancy request for this lecture session.", "info")
        return redirect(url_for('student.dashboard'))

    new_req = AttendanceDiscrepancyRequest(
        student_id=student.id,
        session_id=sess.id,
        reason=reason,
        status='PENDING'
    )
    db.session.add(new_req)
    db.session.commit()

    flash("Discrepancy report submitted to your subject teacher! They will review and update your attendance.", "success")
    return redirect(url_for('student.dashboard'))

@student_bp.route('/student/request_modification', methods=['GET', 'POST'])
@login_required
@student_required
def request_modification():
    student = current_user.student_profile
    if not student:
        flash("Student profile not found.", "danger")
        return redirect(url_for('student.dashboard'))

    if request.method == 'POST':
        session_id = request.form.get('session_id')
        class_name = request.form.get('class_name', '').strip()
        teacher_name = request.form.get('teacher_name', '').strip()
        lecture_time = request.form.get('lecture_time', '').strip()
        reason = request.form.get('reason', '').strip()

        sess_obj = AttendanceSession.query.get(int(session_id)) if (session_id and str(session_id).isdigit()) else None

        if sess_obj:
            if not class_name and sess_obj.class_assigned:
                class_name = sess_obj.class_assigned.name
            if not teacher_name and sess_obj.teacher:
                teacher_name = sess_obj.teacher.name
            if not lecture_time:
                time_str = f"{sess_obj.start_time} - {sess_obj.end_time}" if (sess_obj.start_time and sess_obj.end_time) else (sess_obj.start_time or "Lecture")
                lecture_time = f"{sess_obj.date.strftime('%b %d, %Y')} ({time_str})"

        proof_path = None
        if 'proof_image' in request.files:
            file = request.files['proof_image']
            if file and file.filename != '':
                filename = secure_filename(f"disc_{student.id}_{int(datetime.utcnow().timestamp())}_{file.filename}")
                upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'discrepancy_proofs')
                os.makedirs(upload_folder, exist_ok=True)
                full_path = os.path.join(upload_folder, filename)
                file.save(full_path)
                proof_path = f"static/uploads/discrepancy_proofs/{filename}"

        new_req = AttendanceDiscrepancyRequest(
            student_id=student.id,
            session_id=sess_obj.id if sess_obj else None,
            class_name=class_name or (student.class_assigned.name if student.class_assigned else None),
            teacher_name=teacher_name,
            lecture_time=lecture_time,
            reason=reason,
            image_proof=proof_path,
            status='PENDING'
        )
        db.session.add(new_req)
        db.session.commit()

        flash("Attendance modification request with proof submitted successfully to your teacher!", "success")
        return redirect(url_for('student.request_modification'))

    absent_records = AttendanceRecord.query.filter_by(student_id=student.id, status='ABSENT').order_by(AttendanceRecord.id.desc()).all()
    absent_sessions = [r.session for r in absent_records if r.session]
    if not absent_sessions and student.class_id:
        absent_sessions = AttendanceSession.query.filter_by(class_id=student.class_id, status='COMPLETED').order_by(AttendanceSession.id.desc()).limit(15).all()

    my_requests = AttendanceDiscrepancyRequest.query.filter_by(student_id=student.id).order_by(AttendanceDiscrepancyRequest.id.desc()).all()

    return render_template(
        'student_request_modification.html',
        student=student,
        absent_sessions=absent_sessions,
        my_requests=my_requests
    )


@student_bp.route('/student/notices')
@login_required
@student_required
def notices():
    student = current_user.student_profile
    if not student:
        flash("Student profile not found.", "danger")
        return redirect(url_for('auth.logout'))

    # Get dismissed notice IDs for this specific student
    dismissed_ids = [d.announcement_id for d in StudentDismissedNotice.query.filter_by(student_id=student.id).all()]

    # 1. Admin Notices for Students
    admin_notices_query = ClassAnnouncement.query.filter(
        ClassAnnouncement.posted_by_role == 'admin',
        ClassAnnouncement.target_role.in_(['STUDENTS', 'ALL']),
        (ClassAnnouncement.class_id == None) | (ClassAnnouncement.class_id == student.class_id)
    )
    if dismissed_ids:
        admin_notices_query = admin_notices_query.filter(~ClassAnnouncement.id.in_(dismissed_ids))
    admin_notices = admin_notices_query.order_by(ClassAnnouncement.created_at.desc()).all()

    # Mark active admin notices as read for this student
    read_notice_ids = {r.announcement_id for r in StudentReadNotice.query.filter_by(student_id=student.id).all()}
    for ann in admin_notices:
        if ann.id not in read_notice_ids:
            db.session.add(StudentReadNotice(student_id=student.id, announcement_id=ann.id))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 2. Class Teacher / Faculty Notices
    class_notices_query = ClassAnnouncement.query.filter(
        ClassAnnouncement.posted_by_role == 'teacher',
        (ClassAnnouncement.class_id == student.class_id) | (ClassAnnouncement.class_id == None)
    )
    if dismissed_ids:
        class_notices_query = class_notices_query.filter(~ClassAnnouncement.id.in_(dismissed_ids))
    class_notices = class_notices_query.order_by(ClassAnnouncement.created_at.desc()).all()

    return render_template(
        'student_notices.html',
        student=student,
        admin_notices=admin_notices,
        class_notices=class_notices
    )


@student_bp.route('/student/notice/mark_read/class', methods=['POST'])
@login_required
@student_required
def mark_class_notices_read():
    student = current_user.student_profile
    if not student:
        return jsonify({'success': False}), 403

    dismissed_ids = [d.announcement_id for d in StudentDismissedNotice.query.filter_by(student_id=student.id).all()]
    class_notices_query = ClassAnnouncement.query.filter(
        ClassAnnouncement.posted_by_role == 'teacher',
        (ClassAnnouncement.class_id == student.class_id) | (ClassAnnouncement.class_id == None)
    )
    if dismissed_ids:
        class_notices_query = class_notices_query.filter(~ClassAnnouncement.id.in_(dismissed_ids))
    class_notices = class_notices_query.all()

    read_notice_ids = {r.announcement_id for r in StudentReadNotice.query.filter_by(student_id=student.id).all()}
    for ann in class_notices:
        if ann.id not in read_notice_ids:
            db.session.add(StudentReadNotice(student_id=student.id, announcement_id=ann.id))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({'success': True})


@student_bp.route('/student/notice/dismiss/<int:notice_id>', methods=['POST'])
@login_required
@student_required
def dismiss_notice(notice_id):
    student = current_user.student_profile
    if not student:
        return redirect(url_for('auth.logout'))

    existing = StudentDismissedNotice.query.filter_by(student_id=student.id, announcement_id=notice_id).first()
    if not existing:
        dismissed = StudentDismissedNotice(student_id=student.id, announcement_id=notice_id)
        db.session.add(dismissed)
        db.session.commit()
    flash("Notice dismissed from your dashboard.", "info")
    return redirect(url_for('student.notices'))

