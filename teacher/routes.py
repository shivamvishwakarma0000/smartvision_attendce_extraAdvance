# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - TEACHER PORTAL ROUTING MODULE
# ==============================================================================
# Description: Faculty operations including daily lecture schedules, AI group photo
#              scanning, real-time WebRTC attendance taking, manual roll modification,
#              proxy class substitute duties, leave requests, and reports.
# ==============================================================================

import os
import base64
import uuid
try:
    import face_recognition
except ImportError:
    face_recognition = None
import numpy as np
from datetime import date, datetime, timedelta
from collections import defaultdict
from functools import wraps
from flask import render_template, redirect, url_for, flash, request, Blueprint, jsonify, make_response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db, get_current_date, get_current_time_str, get_current_24h_time_str, get_current_datetime_str
from models import (
    User, Teacher, Class, Subject, Student, Attendance, ClassAnnouncement,
    TeacherAssignment, Timetable, Holiday, TeacherLeave, DailySchedule,
    AttendanceSession, AttendanceRecord, CorrectionRequest, AttendanceDiscrepancyRequest, AttendanceAuditLog,
    ProxyAttendanceTransfer, TeacherDailyAttendance, TeacherAttendanceSettings, TeacherOfficeLocation,
    TeacherDismissedNotice, TeacherReadNotice
)
from schedule_service import generate_daily_schedule, calculate_student_attendance
from auth.routes import save_base64_image

teacher_bp = Blueprint('teacher', __name__)

UPLOAD_FOLDER = 'temp_uploads'
GROUP_PHOTOS_FOLDER = os.path.join(UPLOAD_FOLDER, 'group_photos')
FACES_FOLDER = os.path.join(UPLOAD_FOLDER, 'faces')
os.makedirs(GROUP_PHOTOS_FOLDER, exist_ok=True)

# ==============================================================================
# SECTION 1: ROLE-BASED ACCESS CONTROL & HELPER FUNCTIONS
# ==============================================================================
def teacher_required(f):
    """Restricts route access exclusively to authenticated teacher accounts."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'teacher':
            flash("Teacher access is required to view this page.", "danger")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def get_current_teacher():
    """Retrieves the Teacher ORM profile entity associated with current_user."""
    if hasattr(current_user, 'teacher_profile') and current_user.teacher_profile:
        return current_user.teacher_profile
    return Teacher.query.filter_by(email=current_user.email).first()

# ==============================================================================
# SECTION 2: TEACHER DASHBOARD & TIMETABLE SCHEDULE
# ==============================================================================

@teacher_bp.route('/teacher/dashboard')
@login_required
@teacher_required
def dashboard():
    teacher = get_current_teacher()
    if not teacher:
        flash("Teacher profile not found. Please contact an administrator.", "danger")
        return redirect(url_for('auth.logout'))

    today = get_current_date()
    # Trigger daily schedule resolution for today
    generate_daily_schedule(today)

    # Subjects taught by this teacher (via TeacherAssignment or direct)
    assigned_sub_ids = [asn.subject_id for asn in TeacherAssignment.query.filter_by(teacher_id=teacher.id).all()]
    subjects = Subject.query.filter((Subject.teacher_id == teacher.id) | (Subject.id.in_(assigned_sub_ids) if assigned_sub_ids else False)).all()
    
    directed_classes = Class.query.filter_by(class_teacher_id=teacher.id).all()
    assigned_class_ids = set([sub.class_id for sub in subjects] + [c.id for c in directed_classes])
    assigned_classes = Class.query.filter(Class.id.in_(assigned_class_ids)).all() if assigned_class_ids else []

    # Subject statistics calculated strictly from COMPLETED sessions
    subject_stats = []
    for subject in subjects:
        total_students = Student.query.filter_by(class_id=subject.class_id).count()
        completed_sessions = AttendanceSession.query.filter_by(subject_id=subject.id, status='COMPLETED').all()
        total_sessions = len(completed_sessions)
        
        comp_ids = [s.id for s in completed_sessions]
        total_present = AttendanceRecord.query.filter(
            AttendanceRecord.session_id.in_(comp_ids),
            AttendanceRecord.status == 'PRESENT'
        ).count() if comp_ids else 0

        avg_attendance = round((total_present / (total_sessions * total_students) * 100), 2) if (total_sessions > 0 and total_students > 0) else 0.0

        today_session = AttendanceSession.query.filter_by(subject_id=subject.id, date=today, status='COMPLETED').first()
        today_present = AttendanceRecord.query.filter_by(session_id=today_session.id, status='PRESENT').count() if today_session else 0

        subject_stats.append({
            'subject': subject,
            'class_name': subject.class_assigned.name if subject.class_assigned else 'N/A',
            'total_students': total_students,
            'total_sessions': total_sessions,
            'today_present': today_present,
            'avg_attendance': avg_attendance
        })

    # Today's Classes strictly read from daily_schedule
    today_schedules = DailySchedule.query.join(Timetable).filter(
        DailySchedule.date == today
    ).all()

    todays_classes = []
    for ds in today_schedules:
        tt = ds.timetable
        # Today's classes on Dashboard only show the teacher's own assigned regular classes
        if tt.teacher_id == teacher.id:
            # Existing session for this slot today
            session_rec = AttendanceSession.query.filter(
                (AttendanceSession.daily_schedule_id == ds.id) |
                ((AttendanceSession.timetable_id == tt.id) & (AttendanceSession.date == today)) |
                (
                    (AttendanceSession.subject_id == tt.subject_id) &
                    (AttendanceSession.class_id == tt.class_id) &
                    (AttendanceSession.start_time == tt.start_time) &
                    (AttendanceSession.date == today)
                )
            ).first()
            if session_rec and not session_rec.daily_schedule_id:
                session_rec.daily_schedule_id = ds.id
                db.session.commit()
            session_status = session_rec.status if session_rec else 'SCHEDULED'

            is_cancelled = (ds.is_cancelled or ds.resolved_status == 'CANCELLED')
            can_take_attendance = (
                tt.slot_type == 'CLASS' and 
                not is_cancelled and
                session_status not in ('COMPLETED', 'CANCELLED')
            )

            todays_classes.append({
                'daily_schedule_id': ds.id,
                'timetable_id': tt.id,
                'class_name': tt.class_assigned.name if tt.class_assigned else 'N/A',
                'subject_name': tt.subject_assigned.name if tt.subject_assigned else (tt.slot_type or 'N/A'),
                'room_number': tt.room or 'N/A',
                'start_time': tt.start_time,
                'end_time': tt.end_time,
                'slot_type': tt.slot_type,
                'resolved_status': ds.resolved_status,
                'is_substitute': False,
                'is_proxy': False,
                'is_cancelled': is_cancelled,
                'cancellation_reason': ds.cancellation_reason or "Faculty Absent & No Proxy Available",
                'original_teacher_name': tt.teacher_assigned.name if tt.teacher_assigned else '',
                'session_status': session_status,
                'can_take_attendance': can_take_attendance,
                'session_id': session_rec.id if session_rec else None
            })

    # All timetable slots assigned to teacher
    all_teacher_slots = Timetable.query.filter(
        (Timetable.teacher_id == teacher.id) | (Timetable.subject_id.in_(assigned_sub_ids) if assigned_sub_ids else False)
    ).order_by(Timetable.day_of_week, Timetable.start_time).all()

    current_time_24h = get_current_24h_time_str()

    weekly_timetable_schedule = []
    for slot in all_teacher_slots:
        is_today = (slot.day_of_week == today.strftime('%A'))
        sess_today = AttendanceSession.query.filter(
            (AttendanceSession.timetable_id == slot.id) |
            (
                (AttendanceSession.subject_id == slot.subject_id) &
                (AttendanceSession.class_id == slot.class_id) &
                (AttendanceSession.start_time == slot.start_time)
            ),
            AttendanceSession.date == today,
            AttendanceSession.status == 'COMPLETED'
        ).first() if is_today else None
        
        ds_today = DailySchedule.query.filter_by(date=today, timetable_id=slot.id).first() if is_today else None
        is_cancelled = (ds_today.is_cancelled or ds_today.resolved_status == 'CANCELLED') if ds_today else False
        cancellation_reason = ds_today.cancellation_reason if ds_today else "Faculty Absent & No Proxy Available"

        start_t = slot.start_time or "00:00"
        start_t_clean = start_t.split()[0] if ' ' in start_t else start_t
        not_started_yet = (is_today and current_time_24h < start_t_clean)
        can_take = (is_today and slot.slot_type == 'CLASS' and not sess_today and not not_started_yet and not is_cancelled)

        weekly_timetable_schedule.append({
            'slot': slot,
            'day': slot.day_of_week,
            'period': slot.period_no or 1,
            'class_name': slot.class_assigned.name if slot.class_assigned else 'N/A',
            'subject_name': slot.subject_assigned.name if slot.subject_assigned else (slot.slot_type or 'N/A'),
            'start_time': slot.start_time,
            'end_time': slot.end_time,
            'room_number': slot.room or 'N/A',
            'is_today': is_today,
            'is_proxy': False,
            'is_cancelled': is_cancelled,
            'cancellation_reason': cancellation_reason,
            'not_started_yet': not_started_yet,
            'can_take_attendance': can_take,
            'attendance_marked': (is_today and sess_today is not None),
            'completed_session_id': sess_today.id if sess_today else None
        })

    # Today's Completed Session Logs for Teacher (Scoped to official timetable lecture sessions)
    todays_sessions = AttendanceSession.query.filter_by(
        teacher_id=teacher.id, date=today, status='COMPLETED'
    ).filter(AttendanceSession.timetable_id != None).order_by(AttendanceSession.start_time.asc()).all()
    valid_sessions = todays_sessions

    todays_attendance_log = []
    for sess in valid_sessions:
        sub_name = sess.subject.name if sess.subject else "Subject"
        cls_name = sess.class_assigned.name if sess.class_assigned else ""
        title = f"{sub_name} ({cls_name})" if cls_name else sub_name
        time_info = f"{sess.start_time} - {sess.end_time}" if (sess.start_time and sess.end_time) else ""
        period_no = sess.timetable.period_no if (sess.timetable and sess.timetable.period_no) else None
        slot_label = f"Period {period_no} ({time_info})" if period_no else (time_info if time_info else f"Session #{sess.id}")

        recs = AttendanceRecord.query.filter_by(session_id=sess.id, status='PRESENT').all()
        present_rolls = [r.student.roll_no for r in recs if r.student and r.student.roll_no]
        present_rolls_sorted = sorted(present_rolls, key=lambda x: int(x) if str(x).isdigit() else str(x))
        present_rolls_str = ", ".join(map(str, present_rolls_sorted)) if present_rolls_sorted else "None"

        todays_attendance_log.append({
            'session': sess,
            'title': title,
            'slot_label': slot_label,
            'present_count': len(recs),
            'present_rolls_str': present_rolls_str,
            'records': recs
        })

    # Recent Class Attendance Activity Logs
    sess_ids = [s.id for s in AttendanceSession.query.filter_by(teacher_id=teacher.id, status='COMPLETED').all()]
    recent_records = AttendanceRecord.query.filter(
        AttendanceRecord.session_id.in_(sess_ids)
    ).order_by(AttendanceRecord.id.desc()).limit(20).all() if sess_ids else []

    # Teacher's pending student discrepancy requests
    teacher_sessions = AttendanceSession.query.filter_by(teacher_id=teacher.id).all()
    teacher_session_ids = [s.id for s in teacher_sessions]
    pending_discrepancies = AttendanceDiscrepancyRequest.query.filter(
        AttendanceDiscrepancyRequest.session_id.in_(teacher_session_ids),
        AttendanceDiscrepancyRequest.status == 'PENDING'
    ).order_by(AttendanceDiscrepancyRequest.created_at.desc()).all() if teacher_session_ids else []

    # Teacher's Leave History
    my_leaves = TeacherLeave.query.filter_by(teacher_id=teacher.id).order_by(TeacherLeave.id.desc()).all()
    all_subject_names = [s[0] for s in db.session.query(Subject.name).distinct().order_by(Subject.name).all()]

    return render_template(
        'teacher_dashboard.html',
        teacher=teacher,
        subjects=subjects,
        directed_classes=directed_classes,
        assigned_classes=assigned_classes,
        subject_stats=subject_stats,
        todays_classes=todays_classes,
        weekly_timetable_schedule=weekly_timetable_schedule,
        todays_attendance_log=todays_attendance_log,
        pending_discrepancies=pending_discrepancies,
        recent_records=recent_records,
        my_leaves=my_leaves,
        today_name=today.strftime('%A'),
        all_subject_names=all_subject_names,
        today=today.strftime('%Y-%m-%d')
    )

@teacher_bp.route('/teacher/proxy_classes')
@login_required
@teacher_required
def proxy_classes():
    teacher = get_current_teacher()
    today = get_current_date()

    proxy_leaves = TeacherLeave.query.filter_by(
        substitute_teacher_id=teacher.id,
        status='APPROVED'
    ).order_by(TeacherLeave.date_from.desc()).all()

    proxy_slots = []
    for l in proxy_leaves:
        orig_teacher = l.teacher
        if not orig_teacher:
            continue

        curr = l.date_from
        while curr <= l.date_to:
            day_name = curr.strftime('%A')
            slots_for_day = Timetable.query.filter_by(
                teacher_id=orig_teacher.id,
                day_of_week=day_name,
                slot_type='CLASS'
            ).all()

            for slot in slots_for_day:
                sess_today = AttendanceSession.query.filter_by(
                    timetable_id=slot.id,
                    date=curr,
                    status='COMPLETED'
                ).first()
                is_completed = (sess_today is not None)

                proxy_slots.append({
                    'leave': l,
                    'orig_teacher': orig_teacher,
                    'slot': slot,
                    'duty_date': curr,
                    'class_name': slot.class_assigned.name if slot.class_assigned else 'N/A',
                    'subject_name': slot.subject_assigned.name if slot.subject_assigned else 'N/A',
                    'day': day_name,
                    'start_time': slot.start_time,
                    'end_time': slot.end_time,
                    'room_number': slot.room or 'N/A',
                    'is_active_today': (curr == today),
                    'is_completed': is_completed
                })
            curr += timedelta(days=1)

    # 2. Also fetch direct DailySchedule proxy allocations by admin (for uninformed absences or direct admin assignment)
    direct_proxy_ds = DailySchedule.query.filter_by(
        substitute_teacher_id=teacher.id,
        is_proxy=True
    ).all()
    for pds in direct_proxy_ds:
        if pds.is_cancelled:
            continue
        ptt = pds.timetable
        p_orig_teacher = ptt.teacher_assigned if ptt else None
        
        # Check if already added by leave loop
        already_added = any(ps['slot'].id == ptt.id and ps['duty_date'] == pds.date for ps in proxy_slots)
        if already_added:
            continue
            
        sess_today = AttendanceSession.query.filter(
            (AttendanceSession.daily_schedule_id == pds.id) |
            ((AttendanceSession.timetable_id == ptt.id) & (AttendanceSession.date == pds.date)),
            AttendanceSession.status == 'COMPLETED'
        ).first()
        is_completed = (sess_today is not None)

        proxy_slots.append({
            'leave': None,
            'is_direct_admin_proxy': True,
            'daily_schedule_id': pds.id,
            'orig_teacher': p_orig_teacher,
            'slot': ptt,
            'duty_date': pds.date,
            'class_name': ptt.class_assigned.name if ptt.class_assigned else 'N/A',
            'subject_name': ptt.subject_assigned.name if ptt.subject_assigned else 'N/A',
            'day': ptt.day_of_week,
            'start_time': ptt.start_time,
            'end_time': ptt.end_time,
            'room_number': ptt.room or 'N/A',
            'is_active_today': (pds.date == today),
            'is_completed': is_completed
        })

    received_transfers = ProxyAttendanceTransfer.query.filter(
        ProxyAttendanceTransfer.original_teacher_id == teacher.id,
        ProxyAttendanceTransfer.status.in_(['PENDING', 'SHARED'])
    ).order_by(ProxyAttendanceTransfer.created_at.desc()).all()

    sent_transfers = ProxyAttendanceTransfer.query.filter_by(
        substitute_teacher_id=teacher.id
    ).order_by(ProxyAttendanceTransfer.created_at.desc()).all()

    # Build set of shared slot keys (timetable_id, date) for transfers that are SHARED or APPLIED
    shared_slot_keys = set()
    for t in sent_transfers:
        if t.status in ['SHARED', 'APPLIED'] and t.timetable_id and t.date:
            shared_slot_keys.add((t.timetable_id, t.date))

    return render_template(
        'teacher_proxy_classes.html',
        teacher=teacher,
        proxy_leaves=proxy_leaves,
        proxy_slots=proxy_slots,
        received_transfers=received_transfers,
        sent_transfers=sent_transfers,
        shared_slot_keys=shared_slot_keys,
        today=today
    )


@teacher_bp.route('/teacher/take_proxy_attendance', methods=['GET', 'POST'])
@login_required
@teacher_required
def take_proxy_attendance():
    teacher = get_current_teacher()
    today = get_current_date()

    slot_id = request.args.get('slot_id') or request.form.get('slot_id')
    leave_id = request.args.get('leave_id') or request.form.get('leave_id')
    duty_date_str = request.args.get('duty_date') or request.form.get('duty_date')

    # Resolve slot
    slot = None
    if slot_id and str(slot_id).isdigit():
        slot = Timetable.query.get(int(slot_id))

    if not slot:
        flash("Invalid or missing timetable slot for proxy duty.", "danger")
        return redirect(url_for('teacher.proxy_classes'))

    # Resolve leave
    leave = None
    if leave_id and str(leave_id).isdigit():
        leave = TeacherLeave.query.get(int(leave_id))

    # Resolve duty date
    try:
        duty_date = datetime.strptime(duty_date_str, '%Y-%m-%d').date() if duty_date_str else today
    except Exception:
        duty_date = today

    # Original absent teacher
    orig_teacher = leave.teacher if leave else (Teacher.query.get(slot.teacher_id) if slot.teacher_id else None)

    matched_student_ids = set()
    confidence_scores = {}
    present_rolls_str = ""
    present_count = 0

    if request.method == 'POST':
        class_students = Student.query.filter_by(class_id=slot.class_id).all()

        # Auto-repair face encodings if missing
        for s in class_students:
            if s.face_embedding is None and s.image_filename:
                try:
                    photo_path = os.path.join(FACES_FOLDER, s.image_filename)
                    if not os.path.exists(photo_path):
                        photo_path = os.path.join(current_app.root_path, 'temp_uploads', 'faces', s.image_filename)
                    if os.path.exists(photo_path) and face_recognition is not None:
                        img = face_recognition.load_image_file(photo_path)
                        encs = face_recognition.face_encodings(img)
                        if encs:
                            s.face_embedding = encs[0].tobytes()
                            db.session.commit()
                except Exception as repair_err:
                    print(f"Proxy auto-repair error for student {s.id}: {repair_err}")

        valid_students = [s for s in class_students if s.face_embedding is not None]

        # Fallback: if no students with face embeddings found for this class,
        # use ALL students in the system that have face embeddings
        if not valid_students:
            print(f"[Proxy] No valid embeddings for class_id={slot.class_id}, falling back to all students.")
            all_with_emb = Student.query.filter(Student.face_embedding.isnot(None)).all()
            # Also try to auto-repair those from any class that have images but no embedding
            for s in Student.query.filter(Student.face_embedding.is_(None), Student.image_filename.isnot(None)).all():
                try:
                    photo_path = os.path.join(FACES_FOLDER, s.image_filename)
                    if not os.path.exists(photo_path):
                        photo_path = os.path.join(current_app.root_path, 'temp_uploads', 'faces', s.image_filename)
                    if os.path.exists(photo_path) and face_recognition is not None:
                        img = face_recognition.load_image_file(photo_path)
                        encs = face_recognition.face_encodings(img)
                        if encs:
                            s.face_embedding = encs[0].tobytes()
                            db.session.commit()
                            all_with_emb.append(s)
                except Exception as repair_err:
                    print(f"Proxy global auto-repair error for student {s.id}: {repair_err}")
            valid_students = all_with_emb

        known_encodings = [np.frombuffer(s.face_embedding, dtype=np.float64) for s in valid_students]
        print(f"[Proxy] Face matching pool: {len(valid_students)} students with embeddings.")

        group_photos = request.files.getlist('group_photos') + request.files.getlist('group_photo')
        captured_base64_list = request.form.getlist('captured_base64') + request.form.getlist('captured_images_base64')

        temp_photo_paths = []
        for gp in group_photos:
            if gp and gp.filename:
                filename = secure_filename(f"proxy_{slot.id}_{uuid.uuid4().hex[:6]}_{gp.filename}")
                filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                gp.save(filepath)
                temp_photo_paths.append(filepath)

        for b64 in captured_base64_list:
            if b64 and (';base64,' in b64 or b64.startswith('data:image/')):
                try:
                    imgstr = b64.split(';base64,', 1)[1] if ';base64,' in b64 else b64
                    padding = len(imgstr) % 4
                    if padding:
                        imgstr += '=' * (4 - padding)
                    image_data = base64.b64decode(imgstr)
                    filename = f"proxy_cam_{slot.id}_{uuid.uuid4().hex[:6]}.jpg"
                    filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                    with open(filepath, 'wb') as f:
                        f.write(image_data)
                    temp_photo_paths.append(filepath)
                except Exception as e:
                    print(f"Proxy b64 save error: {e}")

        for filepath in temp_photo_paths:
            try:
                if face_recognition is not None and known_encodings:
                    img = face_recognition.load_image_file(filepath)
                    h, w = img.shape[:2]
                    # Only downscale very large images (>1600px); keep webcam snapshots at full size
                    if max(h, w) > 1600:
                        scaling = 1600.0 / float(max(h, w))
                        new_w, new_h = int(w * scaling), int(h * scaling)
                        try:
                            import cv2
                            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                        except ImportError:
                            from PIL import Image
                            img = np.array(Image.fromarray(img).resize((new_w, new_h)))

                    # upsample=1 improves detection of smaller/distant faces in webcam frames
                    face_locations = face_recognition.face_locations(img, number_of_times_to_upsample=1)
                    unknown_encodings = face_recognition.face_encodings(img, face_locations)
                    print(f"[Proxy] Photo {os.path.basename(filepath)}: {len(face_locations)} face(s) detected.")
                    for unk_enc in unknown_encodings:
                        distances = face_recognition.face_distance(known_encodings, unk_enc)
                        if len(distances) > 0:
                            best_idx = int(np.argmin(distances))
                            best_dist = distances[best_idx]
                            print(f"[Proxy] Best match distance: {best_dist:.3f} -> student id={valid_students[best_idx].id}")
                            if best_dist < 0.55:
                                student_id = valid_students[best_idx].id
                                confidence = round(float(1.0 - best_dist), 2)
                                matched_student_ids.add(student_id)
                                confidence_scores[student_id] = confidence
            except Exception as scan_err:
                print(f"Proxy scan error on {filepath}: {scan_err}")
            finally:
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass

        # Quick roll number manual input
        quick_roll_input = request.form.get('quick_roll_no', '').strip()
        if quick_roll_input:
            roll_list = [r.strip() for r in quick_roll_input.replace(',', ' ').split() if r.strip()]
            for roll in roll_list:
                st = Student.query.filter_by(class_id=slot.class_id, roll_no=roll).first()
                if not st:
                    st = Student.query.filter_by(roll_no=roll).first()
                if st:
                    matched_student_ids.add(st.id)
                    confidence_scores[st.id] = 1.0

        # Build present rolls string
        present_students = Student.query.filter(
            Student.id.in_(matched_student_ids)
        ).all() if matched_student_ids else []
        present_rolls = sorted(
            [s.roll_no for s in present_students if s.roll_no],
            key=lambda x: int(x) if str(x).isdigit() else str(x)
        )
        # ← FIX: assign back so template receives updated values
        present_rolls_str = ",".join(map(str, present_rolls)) if present_rolls else ""
        present_count = len(present_rolls)

        # Absent = all class students not in present set
        all_class = Student.query.filter_by(class_id=slot.class_id).all()
        if not all_class:
            all_class = Student.query.all()  # fallback if class_id mismatch
        absent_students = [s for s in all_class if s.id not in matched_student_ids]
        absent_rolls = sorted(
            [s.roll_no for s in absent_students if s.roll_no],
            key=lambda x: int(x) if str(x).isdigit() else str(x)
        )
        absent_rolls_str = ",".join(map(str, absent_rolls)) if absent_rolls else ""
        absent_count = len(absent_rolls)
        all_students_count = len(all_class)
        print(f"[Proxy] Present: {present_rolls_str} | Absent: {absent_rolls_str}")

        # Auto-save or update ProxyAttendanceTransfer
        if present_rolls_str:
            try:
                transfer = ProxyAttendanceTransfer.query.filter_by(
                    timetable_id=slot.id,
                    date=duty_date
                ).first()
                if not transfer:
                    transfer = ProxyAttendanceTransfer(
                        substitute_teacher_id=teacher.id,
                        original_teacher_id=orig_teacher.id if orig_teacher else teacher.id,
                        leave_id=leave.id if leave else None,
                        timetable_id=slot.id,
                        class_id=slot.class_id,
                        subject_id=slot.subject_id,
                        date=duty_date,
                        time_slot=f"{slot.start_time} - {slot.end_time}",
                        present_rolls=present_rolls_str,
                        status='PENDING'
                    )
                    db.session.add(transfer)
                else:
                    transfer.present_rolls = present_rolls_str
                    transfer.substitute_teacher_id = teacher.id
                    if orig_teacher:
                        transfer.original_teacher_id = orig_teacher.id
                db.session.commit()
                flash(f"✓ Proxy attendance processed: {present_count} student(s) marked Present. Rolls ready to send.", "success")
            except Exception as tr_err:
                db.session.rollback()
                print(f"[Proxy] Error saving proxy transfer: {tr_err}")
    else:
        all_class = Student.query.filter_by(class_id=slot.class_id).all()
        if not all_class:
            all_class = Student.query.all()
        absent_rolls_str = ""
        absent_count = 0
        all_students_count = len(all_class)
        present_students = []

    # Check if there is an existing transfer record with rolls already saved
    if not present_rolls_str:
        saved_tr = ProxyAttendanceTransfer.query.filter_by(
            timetable_id=slot.id,
            date=duty_date
        ).first()
        if saved_tr and saved_tr.present_rolls:
            present_rolls_str = saved_tr.present_rolls
            r_list = [r.strip() for r in present_rolls_str.split(',') if r.strip()]
            present_count = len(r_list)
            present_students = Student.query.filter(Student.roll_no.in_(r_list), Student.class_id == slot.class_id).all()

    return render_template(
        'teacher_take_proxy_attendance.html',
        teacher=teacher,
        slot=slot,
        leave=leave,
        orig_teacher=orig_teacher,
        duty_date=duty_date,
        matched_student_ids=matched_student_ids,
        confidence_scores=confidence_scores,
        present_rolls_str=present_rolls_str,
        present_count=present_count,
        absent_rolls_str=absent_rolls_str,
        absent_count=absent_count,
        all_students_count=all_students_count,
        present_students=present_students
    )


@teacher_bp.route('/teacher/share_proxy_rolls', methods=['POST'])
@login_required
@teacher_required
def share_proxy_rolls():
    teacher = get_current_teacher()
    leave_id = request.form.get('leave_id')
    timetable_id = request.form.get('timetable_id')
    class_id = request.form.get('class_id')
    subject_id = request.form.get('subject_id')
    original_teacher_id = request.form.get('original_teacher_id')
    present_rolls = request.form.get('present_rolls', '').strip()
    duty_date_str = request.form.get('duty_date')

    if not original_teacher_id or not present_rolls:
        flash("Original teacher and present roll numbers are required.", "warning")
        return redirect(url_for('teacher.proxy_classes'))

    try:
        duty_date = datetime.strptime(duty_date_str, '%Y-%m-%d').date() if duty_date_str else get_current_date()
    except Exception:
        duty_date = get_current_date()

    orig_teacher = Teacher.query.get(int(original_teacher_id)) if original_teacher_id else None

    # Clean present rolls to ensure commas only without extra spaces
    clean_present_rolls = ','.join([r.strip() for r in present_rolls.replace(',', ' ').split() if r.strip()])

    # Check if transfer record already exists
    transfer = None
    if timetable_id:
        transfer = ProxyAttendanceTransfer.query.filter_by(
            timetable_id=int(timetable_id),
            date=duty_date
        ).first()

    if not transfer:
        transfer = ProxyAttendanceTransfer(
            substitute_teacher_id=teacher.id,
            original_teacher_id=int(original_teacher_id),
            leave_id=int(leave_id) if leave_id else None,
            timetable_id=int(timetable_id) if timetable_id else None,
            class_id=int(class_id) if class_id else 1,
            subject_id=int(subject_id) if subject_id else 1,
            date=duty_date,
            time_slot=request.form.get('time_slot', 'Lecture Slot'),
            present_rolls=clean_present_rolls,
            status='SHARED'
        )
        db.session.add(transfer)
    else:
        transfer.substitute_teacher_id = teacher.id
        transfer.original_teacher_id = int(original_teacher_id)
        transfer.present_rolls = clean_present_rolls
        transfer.status = 'SHARED'

    try:
        db.session.commit()
        flash(f"Proxy attendance (Rolls: {clean_present_rolls}) successfully sent to {orig_teacher.name if orig_teacher else 'Original Faculty'} inside the app!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error sharing proxy rolls: {e}", "danger")

    return redirect(url_for('teacher.proxy_classes'))


@teacher_bp.route('/teacher/dismiss_proxy_transfer/<int:transfer_id>', methods=['POST', 'GET'])
@login_required
@teacher_required
def dismiss_proxy_transfer(transfer_id):
    teacher = get_current_teacher()
    transfer = ProxyAttendanceTransfer.query.get_or_404(transfer_id)
    if transfer.original_teacher_id == teacher.id or transfer.substitute_teacher_id == teacher.id:
        db.session.delete(transfer)
        try:
            db.session.commit()
            flash("Proxy attendance record deleted/dismissed successfully.", "info")
        except Exception as e:
            db.session.rollback()
            flash(f"Error deleting record: {e}", "danger")
    return redirect(request.referrer or url_for('teacher.leave_applications'))


@teacher_bp.route('/teacher/update_proxy_rolls/<int:transfer_id>', methods=['POST'])
@login_required
@teacher_required
def update_proxy_rolls(transfer_id):
    teacher = get_current_teacher()
    transfer = ProxyAttendanceTransfer.query.get_or_404(transfer_id)
    if transfer.original_teacher_id == teacher.id or transfer.substitute_teacher_id == teacher.id:
        raw_rolls = request.form.get('present_rolls', '').strip()
        clean_rolls = ','.join([r.strip() for r in raw_rolls.replace(',', ' ').split() if r.strip()])
        transfer.present_rolls = clean_rolls
        try:
            db.session.commit()
            flash("Present roll numbers updated successfully!", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating roll numbers: {e}", "danger")
    return redirect(request.referrer or url_for('teacher.leave_applications'))


@teacher_bp.route('/teacher/apply_proxy_rolls/<int:transfer_id>', methods=['POST'])
@login_required
@teacher_required
def apply_proxy_rolls(transfer_id):
    teacher = get_current_teacher()
    transfer = ProxyAttendanceTransfer.query.get_or_404(transfer_id)

    if transfer.original_teacher_id != teacher.id:
        flash("Access Denied: You can only apply proxy attendance sent to you.", "danger")
        return redirect(url_for('teacher.dashboard'))

    # Locate or create AttendanceSession for the specific timetable slot
    tt = Timetable.query.get(transfer.timetable_id) if transfer.timetable_id else None
    ds = DailySchedule.query.filter_by(date=transfer.date, timetable_id=tt.id).first() if tt else None

    session_rec = None
    if transfer.timetable_id:
        session_rec = AttendanceSession.query.filter_by(timetable_id=transfer.timetable_id, date=transfer.date).first()
    if not session_rec and ds:
        session_rec = AttendanceSession.query.filter_by(daily_schedule_id=ds.id).first()

    start_t = tt.start_time if tt else (transfer.time_slot.split('-')[0].strip() if '-' in transfer.time_slot else "09:00")
    end_t = tt.end_time if tt else (transfer.time_slot.split('-')[1].strip() if '-' in transfer.time_slot else "09:50")

    if not session_rec and tt:
        session_rec = AttendanceSession.query.filter_by(
            teacher_id=teacher.id,
            class_id=transfer.class_id,
            subject_id=transfer.subject_id,
            date=transfer.date,
            start_time=start_t
        ).first()

    if not session_rec:
        session_rec = AttendanceSession(
            timetable_id=transfer.timetable_id,
            daily_schedule_id=ds.id if ds else None,
            date=transfer.date,
            teacher_id=teacher.id,
            class_id=transfer.class_id,
            subject_id=transfer.subject_id,
            start_time=start_t,
            end_time=end_t,
            status='ATTENDANCE_OPEN'
        )
        db.session.add(session_rec)
        db.session.flush()
    else:
        if tt:
            session_rec.timetable_id = tt.id
            session_rec.start_time = tt.start_time
            session_rec.end_time = tt.end_time
        if ds:
            session_rec.daily_schedule_id = ds.id

    # Check if edited present_rolls were submitted directly from the card
    edited_rolls = request.form.get('present_rolls')
    if edited_rolls:
        clean_rolls = ','.join([r.strip() for r in edited_rolls.replace(',', ' ').split() if r.strip()])
        transfer.present_rolls = clean_rolls

    class_students = Student.query.filter_by(class_id=transfer.class_id).all()
    present_rolls = [r.strip() for r in transfer.present_rolls.replace(',', ' ').split() if r.strip()]

    present_count = 0
    for student in class_students:
        is_pres = (student.roll_no in present_rolls or str(student.roll_no) in present_rolls)
        status_val = 'PRESENT' if is_pres else 'ABSENT'
        if is_pres:
            present_count += 1

        rec = AttendanceRecord.query.filter_by(session_id=session_rec.id, student_id=student.id).first()
        if not rec:
            rec = AttendanceRecord(
                session_id=session_rec.id,
                student_id=student.id,
                status=status_val,
                confidence=1.0 if is_pres else 0.0,
                marked_by='PROXY',
                marked_at=datetime.utcnow()
            )
            db.session.add(rec)
        else:
            rec.status = status_val
            rec.marked_at = datetime.utcnow()

        if is_pres:
            leg_att = Attendance.query.filter_by(student_id=student.id, date=transfer.date, subject_id=transfer.subject_id).first()
            if not leg_att:
                leg_att = Attendance(
                    student_id=student.id,
                    date=transfer.date,
                    status='Present',
                    subject_id=transfer.subject_id,
                    time_marked=get_current_time_str()
                )
                db.session.add(leg_att)
            else:
                leg_att.status = 'Present'

    session_rec.status = 'COMPLETED'
    transfer.status = 'APPLIED'

    try:
        db.session.commit()
        flash(f"Proxy attendance from {transfer.substitute_teacher.name if transfer.substitute_teacher else 'Proxy Faculty'} applied successfully! Marked {present_count} student(s) Present.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error applying proxy rolls: {e}", "danger")

    return redirect(url_for('teacher.dashboard'))


@teacher_bp.route('/teacher/leave_applications', methods=['GET'])
@login_required
@teacher_required
def leave_applications():
    teacher = get_current_teacher()
    today = get_current_date()
    my_leaves = TeacherLeave.query.filter_by(teacher_id=teacher.id).order_by(TeacherLeave.id.desc()).all()

    active_leaves = [l for l in my_leaves if l.status == 'PENDING' or (l.status == 'APPROVED' and l.date_to >= today)]
    past_leaves = [l for l in my_leaves if l not in active_leaves]

    # Proxy transfers received while this teacher was on leave (only active SHARED transfers)
    received_proxy_transfers = ProxyAttendanceTransfer.query.filter_by(
        original_teacher_id=teacher.id,
        status='SHARED'
    ).order_by(ProxyAttendanceTransfer.created_at.desc()).all()

    return render_template(
        'teacher_leave_applications.html',
        teacher=teacher,
        my_leaves=my_leaves,
        active_leaves=active_leaves,
        past_leaves=past_leaves,
        received_proxy_transfers=received_proxy_transfers,
        today=today
    )

@teacher_bp.route('/teacher/apply_leave', methods=['POST'])
@login_required
@teacher_required
def apply_leave():
    teacher = get_current_teacher()
    date_from_str = request.form.get('date_from')
    date_to_str = request.form.get('date_to')
    leave_type = request.form.get('leave_type', 'FULL')
    reason = request.form.get('reason', '').strip()

    if not date_from_str or not date_to_str:
        flash("Date From and Date To are required.", "warning")
        return redirect(url_for('teacher.leave_applications'))

    d_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
    d_to = datetime.strptime(date_to_str, "%Y-%m-%d").date()

    if d_to < d_from:
        flash("End date cannot be earlier than start date.", "warning")
        return redirect(url_for('teacher.leave_applications'))

    try:
        leave = TeacherLeave(
            teacher_id=teacher.id,
            date_from=d_from,
            date_to=d_to,
            leave_type=leave_type,
            reason=reason,
            status='PENDING'
        )
        db.session.add(leave)
        db.session.commit()
        flash("Leave application submitted successfully! Pending admin approval.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error submitting leave application: {e}", "danger")

    return redirect(url_for('teacher.leave_applications'))

@teacher_bp.route('/teacher/take_attendance', methods=['GET', 'POST'])
@login_required
@teacher_required
def take_attendance():
    teacher = get_current_teacher()
    today = get_current_date()

    daily_schedule_id = request.args.get('daily_schedule_id') or request.form.get('daily_schedule_id')
    timetable_id = request.args.get('timetable_id') or request.args.get('slot_id') or request.form.get('timetable_id')
    session_id = request.args.get('session_id') or request.form.get('session_id')
    subject_id_arg = request.args.get('subject_id') or request.form.get('subject_id')

    session_rec = None
    if session_id and str(session_id).isdigit():
        session_rec = AttendanceSession.query.get(int(session_id))

    if not session_rec and daily_schedule_id and str(daily_schedule_id).isdigit():
        ds = DailySchedule.query.get(int(daily_schedule_id))
        if ds:
            session_rec = AttendanceSession.query.filter_by(daily_schedule_id=ds.id).first()
            if not session_rec and ds.timetable.slot_type == 'CLASS':
                session_rec = AttendanceSession(
                    timetable_id=ds.timetable_id,
                    daily_schedule_id=ds.id,
                    date=today,
                    teacher_id=teacher.id,
                    class_id=ds.timetable.class_id,
                    subject_id=ds.timetable.subject_id,
                    start_time=ds.timetable.start_time,
                    end_time=ds.timetable.end_time,
                    status='ATTENDANCE_OPEN'
                )
                db.session.add(session_rec)
                db.session.commit()

    if not session_rec and timetable_id and str(timetable_id).isdigit():
        tt = Timetable.query.get(int(timetable_id))
        if tt and tt.slot_type == 'CLASS':
            ds_today = DailySchedule.query.filter_by(timetable_id=tt.id, date=today).first()
            session_rec = AttendanceSession.query.filter_by(timetable_id=tt.id, date=today).first()
            if not session_rec:
                session_rec = AttendanceSession(
                    timetable_id=tt.id,
                    daily_schedule_id=ds_today.id if ds_today else None,
                    date=today,
                    teacher_id=teacher.id,
                    class_id=tt.class_id,
                    subject_id=tt.subject_id,
                    start_time=tt.start_time,
                    end_time=tt.end_time,
                    status='ATTENDANCE_OPEN'
                )
                db.session.add(session_rec)
                db.session.commit()
            elif ds_today and not session_rec.daily_schedule_id:
                session_rec.daily_schedule_id = ds_today.id
                db.session.commit()

    if not session_rec and not timetable_id and subject_id_arg and str(subject_id_arg).isdigit():
        sub_obj = Subject.query.get(int(subject_id_arg))
        if sub_obj:
            session_rec = AttendanceSession.query.filter_by(subject_id=sub_obj.id, date=today).first()

    # Block attendance if this slot was cancelled today by admin
    if session_rec and session_rec.daily_schedule and session_rec.daily_schedule.is_cancelled:
        flash(f"This class period has been cancelled for today ({session_rec.daily_schedule.cancellation_reason or 'Faculty unavailable'}). Attendance cannot be marked.", "warning")
        return redirect(url_for('teacher.dashboard'))
    if timetable_id and str(timetable_id).isdigit():
        ds_chk = DailySchedule.query.filter_by(timetable_id=int(timetable_id), date=today).first()
        if ds_chk and ds_chk.is_cancelled:
            flash(f"This class period has been cancelled for today ({ds_chk.cancellation_reason or 'Faculty unavailable'}). Attendance cannot be marked.", "warning")
            return redirect(url_for('teacher.dashboard'))

    is_sandbox_mode = (session_rec is None)
    subjects = Subject.query.filter_by(teacher_id=teacher.id).all()
    results = None
    matched_student_ids = set()
    confidence_scores = {}
    sandbox_matches = []

    from extensions import IST
    current_hour_ist = datetime.now(IST).hour
    is_same_day_editable = (session_rec and session_rec.date == today and current_hour_ist < 23)

    if request.method == 'POST':
        group_photos = request.files.getlist('group_photos') + request.files.getlist('group_photo')
        captured_base64_list = request.form.getlist('captured_base64') + request.form.getlist('captured_images_base64')

        # 1. SANDBOX / PRACTICE SCAN MODE (Standalone - NOT linked to a timetable slot)
        if is_sandbox_mode:
            target_class_id = request.form.get('class_id')
            if target_class_id and str(target_class_id).isdigit():
                class_students = Student.query.filter_by(class_id=int(target_class_id)).all()
            else:
                class_students = Student.query.all()

            valid_students = [s for s in class_students if s.face_embedding is not None]
            known_encodings = [np.frombuffer(s.face_embedding, dtype=np.float64) for s in valid_students]

            temp_photo_paths = []
            for gp in group_photos:
                if gp and gp.filename:
                    filename = secure_filename(f"sandbox_{uuid.uuid4().hex[:6]}_{gp.filename}")
                    filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                    gp.save(filepath)
                    temp_photo_paths.append(filepath)

            for b64 in captured_base64_list:
                if b64 and (';base64,' in b64 or b64.startswith('data:image/')):
                    try:
                        imgstr = b64.split(';base64,', 1)[1] if ';base64,' in b64 else b64
                        padding = len(imgstr) % 4
                        if padding:
                            imgstr += '=' * (4 - padding)
                        image_data = base64.b64decode(imgstr)
                        filename = f"cam_sandbox_{uuid.uuid4().hex[:6]}.jpg"
                        filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                        with open(filepath, 'wb') as f:
                            f.write(image_data)
                        temp_photo_paths.append(filepath)
                    except Exception as e:
                        print(f"Error saving sandbox b64 frame: {e}")

            for filepath in temp_photo_paths:
                try:
                    if face_recognition is not None and known_encodings:
                        img = face_recognition.load_image_file(filepath)
                        h, w = img.shape[:2]
                        if max(h, w) > 800:
                            scaling = 800.0 / float(max(h, w))
                            new_w, new_h = int(w * scaling), int(h * scaling)
                            try:
                                import cv2
                                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                            except ImportError:
                                from PIL import Image
                                img = np.array(Image.fromarray(img).resize((new_w, new_h)))

                        unknown_encodings = face_recognition.face_encodings(img)
                        for unk_enc in unknown_encodings:
                            distances = face_recognition.face_distance(known_encodings, unk_enc)
                            if len(distances) > 0:
                                best_idx = np.argmin(distances)
                                best_dist = distances[best_idx]
                                if best_dist < 0.70:
                                    st_obj = valid_students[best_idx]
                                    matched_student_ids.add(st_obj.id)
                                    confidence_scores[st_obj.id] = round(float(1.0 - best_dist), 2)
                except Exception as e:
                    print(f"Sandbox face scan error: {e}")
                finally:
                    if os.path.exists(filepath):
                        try: os.remove(filepath)
                        except Exception: pass

            sandbox_matches = [s for s in class_students if s.id in matched_student_ids]
            flash(f"⚡ [Sandbox Test Scan]: Identified {len(sandbox_matches)} student(s). This is a test scan only and is NOT recorded into official database attendance logs.", "info")

        # 2. OFFICIAL TIMETABLE SESSION ATTENDANCE
        elif session_rec:
            if session_rec.status == 'COMPLETED' and not is_same_day_editable:
                flash("This session is COMPLETED and locked (past day or after 11 PM IST). Submit a correction request to edit.", "warning")
                return redirect(url_for('teacher.dashboard'))

            class_students = Student.query.filter_by(class_id=session_rec.class_id).all()

            # Auto-repair face encodings if missing
            for s in class_students:
                if s.face_embedding is None and s.image_filename:
                    try:
                        photo_path = os.path.join(FACES_FOLDER, s.image_filename)
                        if not os.path.exists(photo_path):
                            photo_path = os.path.join(current_app.root_path, 'temp_uploads', 'faces', s.image_filename)
                        if os.path.exists(photo_path) and face_recognition is not None:
                            img = face_recognition.load_image_file(photo_path)
                            encs = face_recognition.face_encodings(img)
                            if encs:
                                s.face_embedding = encs[0].tobytes()
                                db.session.commit()
                    except Exception as repair_err:
                        print(f"Auto-repair error for student {s.id}: {repair_err}")

            valid_students = [s for s in class_students if s.face_embedding is not None]
            known_encodings = [np.frombuffer(s.face_embedding, dtype=np.float64) for s in valid_students]

            temp_photo_paths = []
            for gp in group_photos:
                if gp and gp.filename:
                    filename = secure_filename(f"session_{session_rec.id}_{uuid.uuid4().hex[:6]}_{gp.filename}")
                    filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                    gp.save(filepath)
                    temp_photo_paths.append(filepath)

            for b64 in captured_base64_list:
                if b64 and (';base64,' in b64 or b64.startswith('data:image/')):
                    try:
                        imgstr = b64.split(';base64,', 1)[1] if ';base64,' in b64 else b64
                        padding = len(imgstr) % 4
                        if padding:
                            imgstr += '=' * (4 - padding)
                        image_data = base64.b64decode(imgstr)
                        filename = f"cam_session_{session_rec.id}_{uuid.uuid4().hex[:6]}.jpg"
                        filepath = os.path.join(GROUP_PHOTOS_FOLDER, filename)
                        with open(filepath, 'wb') as f:
                            f.write(image_data)
                        temp_photo_paths.append(filepath)
                    except Exception as e:
                        print(f"Error saving b64 camera frame: {e}")

            for filepath in temp_photo_paths:
                try:
                    if face_recognition is not None and known_encodings:
                        img = face_recognition.load_image_file(filepath)
                        h, w = img.shape[:2]
                        if max(h, w) > 800:
                            scaling = 800.0 / float(max(h, w))
                            new_w, new_h = int(w * scaling), int(h * scaling)
                            try:
                                import cv2
                                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                            except ImportError:
                                from PIL import Image
                                img = np.array(Image.fromarray(img).resize((new_w, new_h)))

                        unknown_encodings = face_recognition.face_encodings(img)
                        for unk_enc in unknown_encodings:
                            distances = face_recognition.face_distance(known_encodings, unk_enc)
                            if len(distances) > 0:
                                best_idx = np.argmin(distances)
                                best_dist = distances[best_idx]
                                if best_dist < 0.70:
                                    student_id = valid_students[best_idx].id
                                    confidence = round(float(1.0 - best_dist), 2)
                                    matched_student_ids.add(student_id)
                                    confidence_scores[student_id] = confidence
                except Exception as scan_err:
                    print(f"Scan error on {filepath}: {scan_err}")
                finally:
                    if os.path.exists(filepath):
                        try: os.remove(filepath)
                        except Exception: pass

            # Check if quick_roll_no input was provided in Camera mode
            quick_roll_input = request.form.get('quick_roll_no', '').strip()
            if quick_roll_input and session_rec:
                roll_list = [r.strip() for r in quick_roll_input.replace(',', ' ').split() if r.strip()]
                for roll in roll_list:
                    st = Student.query.filter_by(class_id=session_rec.class_id, roll_no=roll).first()
                    if not st:
                        st = Student.query.filter_by(roll_no=roll).first()
                    if st:
                        matched_student_ids.add(st.id)
                        confidence_scores[st.id] = 1.0

            # Automatically record attendance and lock session on face scan submit
            for student in class_students:
                status_val = 'PRESENT' if student.id in matched_student_ids else 'ABSENT'
                conf_float = confidence_scores.get(student.id, 1.0 if status_val == 'PRESENT' else 0.0)

                existing_rec = AttendanceRecord.query.filter_by(session_id=session_rec.id, student_id=student.id).first()
                if not existing_rec:
                    rec = AttendanceRecord(
                        session_id=session_rec.id,
                        student_id=student.id,
                        status=status_val,
                        confidence=conf_float,
                        marked_by='CAMERA' if status_val == 'PRESENT' else 'MANUAL',
                        marked_at=datetime.utcnow()
                    )
                    db.session.add(rec)
                else:
                    existing_rec.status = status_val
                    existing_rec.confidence = conf_float
                    existing_rec.marked_at = datetime.utcnow()

                if status_val == 'PRESENT':
                    leg_att = Attendance.query.filter_by(student_id=student.id, date=session_rec.date, subject_id=session_rec.subject_id).first()
                    if not leg_att:
                        leg_att = Attendance(
                            student_id=student.id,
                            date=session_rec.date,
                            status='Present',
                            subject_id=session_rec.subject_id,
                            time_marked=get_current_time_str()
                        )
                        db.session.add(leg_att)
                    else:
                        leg_att.status = 'Present'

            session_rec.status = 'COMPLETED'
            try:
                db.session.commit()
                flash(f"Official lecture attendance marked! Session COMPLETED with {len(matched_student_ids)} student(s) Present.", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Error saving attendance session: {e}", "danger")

            return redirect(url_for('teacher.take_attendance', session_id=session_rec.id))

    class_students = Student.query.filter_by(class_id=session_rec.class_id).all() if session_rec else []
    saved_records = AttendanceRecord.query.filter_by(session_id=session_rec.id).all() if session_rec else []
    saved_records_map = {r.student_id: r.status for r in saved_records}

    present_recs = [r for r in saved_records if r.status == 'PRESENT']
    present_rolls = [r.student.roll_no for r in present_recs if r.student and r.student.roll_no]
    present_rolls_sorted = sorted(present_rolls, key=lambda x: int(x) if str(x).isdigit() else str(x))
    present_rolls_str = ", ".join(map(str, present_rolls_sorted)) if present_rolls_sorted else "None"

    if saved_records:
        absent_recs = [r for r in saved_records if r.status == 'ABSENT']
        absent_rolls = [r.student.roll_no for r in absent_recs if r.student and r.student.roll_no]
        absent_rolls_sorted = sorted(absent_rolls, key=lambda x: int(x) if str(x).isdigit() else str(x))
        absent_rolls_str = ", ".join(map(str, absent_rolls_sorted)) if absent_rolls_sorted else "None"
        absent_count = len(absent_rolls_sorted)
    else:
        absent_rolls_str = "Not Marked Yet (Session Open)"
        absent_count = 0

    today_teacher_sessions = AttendanceSession.query.filter_by(teacher_id=teacher.id, date=today).order_by(AttendanceSession.start_time.asc()).all()
    all_classes = Class.query.order_by(Class.name).all()

    return render_template(
        'teacher_take_attendance.html',
        session=session_rec,
        is_sandbox_mode=is_sandbox_mode,
        sandbox_matches=sandbox_matches,
        all_classes=all_classes,
        class_students=class_students,
        matched_student_ids=matched_student_ids,
        confidence_scores=confidence_scores,
        saved_records_map=saved_records_map,
        present_count=len(present_recs),
        present_rolls_str=present_rolls_str,
        absent_count=absent_count,
        absent_rolls_str=absent_rolls_str,
        today_teacher_sessions=today_teacher_sessions,
        subjects=subjects,
        is_same_day_editable=is_same_day_editable,
        today=today.strftime('%Y-%m-%d')
    )

@teacher_bp.route('/teacher/confirm_attendance/<int:session_id>', methods=['POST'])
@login_required
@teacher_required
def confirm_attendance(session_id):
    session_rec = AttendanceSession.query.get_or_404(session_id)
    today = get_current_date()
    from extensions import IST
    current_hour_ist = datetime.now(IST).hour
    is_same_day_editable = (session_rec.date == today and current_hour_ist < 23)

    if session_rec.status == 'COMPLETED' and not is_same_day_editable:
        flash("Session is completed and locked (past day or after 11 PM IST). Submit a correction request to edit.", "warning")
        return redirect(url_for('teacher.dashboard'))

    class_students = Student.query.filter_by(class_id=session_rec.class_id).all()
    
    present_count = 0
    for student in class_students:
        status_val = request.form.get(f'student_status_{student.id}', 'ABSENT').upper()
        conf_val = request.form.get(f'confidence_{student.id}', '1.0')
        try:
            conf_float = float(conf_val)
        except ValueError:
            conf_float = 1.0

        existing_rec = AttendanceRecord.query.filter_by(session_id=session_rec.id, student_id=student.id).first()
        if not existing_rec:
            rec = AttendanceRecord(
                session_id=session_rec.id,
                student_id=student.id,
                status=status_val,
                confidence=conf_float,
                marked_by='CAMERA' if conf_float < 1.0 else 'MANUAL',
                marked_at=datetime.utcnow()
            )
            db.session.add(rec)
        else:
            existing_rec.status = status_val
            existing_rec.confidence = conf_float
            existing_rec.marked_at = datetime.utcnow()

        if status_val == 'PRESENT':
            present_count += 1
            leg_att = Attendance.query.filter_by(student_id=student.id, date=session_rec.date, subject_id=session_rec.subject_id).first()
            if not leg_att:
                leg_att = Attendance(
                    student_id=student.id,
                    date=session_rec.date,
                    status='Present',
                    subject_id=session_rec.subject_id,
                    time_marked=get_current_time_str()
                )
                db.session.add(leg_att)
            else:
                leg_att.status = 'Present'

    session_rec.status = 'COMPLETED'
    try:
        db.session.commit()
        flash(f"Session COMPLETED and locked! Marked {present_count} student(s) Present.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error locking attendance session: {e}", "danger")

    return redirect(url_for('teacher.take_attendance', session_id=session_rec.id))

@teacher_bp.route('/teacher/request_correction', methods=['POST'])
@login_required
@teacher_required
def request_correction():
    session_id = request.form.get('session_id')
    reason = request.form.get('reason', '').strip()

    if not session_id or not reason:
        flash("Session ID and reason are required for correction request.", "warning")
        return redirect(url_for('teacher.dashboard'))

    try:
        req = CorrectionRequest(
            session_id=int(session_id),
            requested_by=current_user.id,
            reason=reason,
            status='PENDING'
        )
        db.session.add(req)
        db.session.commit()
        flash("Correction request submitted successfully! Awaiting admin review.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error submitting correction request: {e}", "danger")

    return redirect(url_for('teacher.dashboard'))

@teacher_bp.route('/teacher/update_preferences', methods=['POST'])
@login_required
@teacher_required
def update_preferences():
    teacher = get_current_teacher()
    primary = request.form.get('primary_subject', '').strip()
    secondary = request.form.get('secondary_subject', '').strip()
    tertiary = request.form.get('tertiary_subject', '').strip()
    try:
        teacher.primary_subject = primary or None
        teacher.secondary_subject = secondary or None
        teacher.tertiary_subject = tertiary or None
        db.session.commit()
        flash("Teaching expertise preferences updated successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating preferences: {e}", "danger")

    return redirect(url_for('teacher.dashboard'))

@teacher_bp.route('/teacher/reports')
@login_required
@teacher_required
def reports():
    teacher = get_current_teacher()
    subjects = Subject.query.filter_by(teacher_id=teacher.id).all()
    selected_subject_id = request.args.get('subject_id')

    selected_subject = None
    records = []
    if selected_subject_id and selected_subject_id.isdigit():
        selected_subject = Subject.query.get(int(selected_subject_id))

    if selected_subject and selected_subject.teacher_id == teacher.id:
        sessions = AttendanceSession.query.filter_by(subject_id=selected_subject.id, status='COMPLETED').all()
        s_ids = [s.id for s in sessions]
        records = AttendanceRecord.query.filter(AttendanceRecord.session_id.in_(s_ids)).all() if s_ids else []
    else:
        sub_ids = [s.id for s in subjects]
        sessions = AttendanceSession.query.filter(AttendanceSession.subject_id.in_(sub_ids), AttendanceSession.status == 'COMPLETED').all() if sub_ids else []
        s_ids = [s.id for s in sessions]
        records = AttendanceRecord.query.filter(AttendanceRecord.session_id.in_(s_ids)).all() if s_ids else []

    return render_template('teacher_reports.html', subjects=subjects, records=records, selected_subject=selected_subject)

@teacher_bp.route('/teacher/view_reports')
@login_required
@teacher_required
def view_reports():
    return reports()

@teacher_bp.route('/teacher/enrolled_students')
@login_required
@teacher_required
def enrolled_students():
    teacher = get_current_teacher()
    subjects = Subject.query.filter_by(teacher_id=teacher.id).all()
    class_ids = set([s.class_id for s in subjects if s.class_id])
    students = Student.query.filter(Student.class_id.in_(class_ids)).all() if class_ids else []
    
    for st in students:
        stats = calculate_student_attendance(st.id)
        st.calculated_pct = stats['percentage']

    return render_template('teacher_enrolled_students.html', students=students)

@teacher_bp.route('/teacher/class_teacher_dashboard')
@login_required
@teacher_required
def class_teacher_dashboard():
    teacher = get_current_teacher()
    directed_class = Class.query.filter_by(class_teacher_id=teacher.id).first()
    if not directed_class:
        flash("Access Restricted: You are not assigned as a Class Teacher for any class.", "warning")
        return redirect(url_for('teacher.dashboard'))

    students = Student.query.filter_by(class_id=directed_class.id).all() if directed_class else []
    
    student_stats = []
    total_pct = 0
    for st in students:
        stats = calculate_student_attendance(st.id)
        pct = stats['percentage']
        st.calculated_pct = pct
        total_pct += pct
        student_stats.append({
            'student': st,
            'pct': pct,
            'parent_name': st.parent_name or 'N/A',
            'parent_email': st.parent_email or 'N/A',
            'parent_phone': st.parent_mobile or 'N/A'
        })

    class_avg_pct = round(total_pct / len(students), 2) if students else 0
    announcements = ClassAnnouncement.query.filter_by(class_id=directed_class.id).order_by(ClassAnnouncement.created_at.desc()).all() if directed_class else []

    class_retention_students = [st for st in students if getattr(st, 'calculated_pct', 100) < 75]

    return render_template('class_teacher_dashboard.html', 
                           teacher=teacher, 
                           directed_class=directed_class, 
                           students=students, 
                           student_stats=student_stats,
                           class_avg_pct=class_avg_pct, 
                           announcements=announcements, 
                           class_retention_students=class_retention_students)

@teacher_bp.route('/teacher/post_announcement', methods=['POST'])
@login_required
@teacher_required
def post_announcement():
    teacher = get_current_teacher()
    class_id = request.form.get('class_id')
    title = request.form.get('title')
    notice_type = request.form.get('notice_type', 'Announcement')
    content = request.form.get('content')

    if class_id and title and content:
        ann = ClassAnnouncement(
            class_id=int(class_id),
            teacher_id=teacher.id,
            title=title,
            notice_type=notice_type,
            content=content
        )
        db.session.add(ann)
        db.session.commit()
        flash('Announcement published successfully!', 'success')

    return redirect(url_for('teacher.class_teacher_dashboard'))

@teacher_bp.route('/teacher/delete_announcement/<int:notice_id>', methods=['POST'])
@login_required
@teacher_required
def delete_announcement(notice_id):
    ann = ClassAnnouncement.query.get_or_404(notice_id)
    db.session.delete(ann)
    db.session.commit()
    flash('Announcement deleted.', 'info')
    return redirect(url_for('teacher.class_teacher_dashboard'))

@teacher_bp.route('/teacher/download_report')
@login_required
@teacher_required
def download_report():
    import io, csv
    from flask import Response
    teacher = get_current_teacher()
    subject_id = request.args.get('subject_id')
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Session ID', 'Date', 'Subject', 'Student Roll No', 'Student Name', 'Status', 'Marked At'])

    query = AttendanceRecord.query.join(AttendanceSession)
    if subject_id and subject_id.isdigit():
        query = query.filter(AttendanceSession.subject_id == int(subject_id))
    else:
        sub_ids = [s.id for s in Subject.query.filter_by(teacher_id=teacher.id).all()]
        query = query.filter(AttendanceSession.subject_id.in_(sub_ids)) if sub_ids else []

    if query:
        for rec in query.all():
            writer.writerow([
                rec.session_id,
                rec.session.date.strftime('%Y-%m-%d') if rec.session else '',
                rec.session.subject.name if (rec.session and rec.session.subject) else '',
                rec.student.roll_no if rec.student else '',
                rec.student.name if rec.student else '',
                rec.status,
                rec.marked_at.strftime('%Y-%m-%d %H:%M:%S') if rec.marked_at else ''
            ])

    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers["Content-Disposition"] = "attachment; filename=attendance_report.csv"
    return response

@teacher_bp.route('/teacher/timetable')
@login_required
@teacher_required
def timetable():
    teacher = get_current_teacher()
    classes = Class.query.order_by(Class.name).all()
    
    selected_class_id_str = request.args.get('class_id')
    selected_class_id = None
    if selected_class_id_str and selected_class_id_str.isdigit():
        selected_class_id = int(selected_class_id_str)
    
    if not selected_class_id:
        directed_class = Class.query.filter_by(class_teacher_id=teacher.id).first()
        if directed_class:
            selected_class_id = directed_class.id
        elif classes:
            assigned_sub = Subject.query.filter_by(teacher_id=teacher.id).first()
            if assigned_sub and assigned_sub.class_id:
                selected_class_id = assigned_sub.class_id
            else:
                selected_class_id = classes[0].id

    selected_class = Class.query.get(selected_class_id) if selected_class_id else None

    query = Timetable.query
    if selected_class_id:
        query = query.filter_by(class_id=selected_class_id)

    timetable_entries = query.order_by(Timetable.day_of_week, Timetable.start_time).all()

    from main.routes import get_or_create_period_settings
    period_settings = get_or_create_period_settings()

    return render_template(
        'teacher_timetable.html',
        timetable_entries=timetable_entries,
        classes=classes,
        selected_class=selected_class,
        selected_class_id=selected_class_id,
        period_settings=period_settings,
        teacher=teacher
    )

@teacher_bp.route('/teacher/approve_discrepancy/<int:req_id>', methods=['POST'])
@login_required
@teacher_required
def approve_discrepancy(req_id):
    disc_req = AttendanceDiscrepancyRequest.query.get_or_404(req_id)
    disc_req.status = 'APPROVED'
    
    rec = AttendanceRecord.query.filter_by(session_id=disc_req.session_id, student_id=disc_req.student_id).first()
    if not rec:
        rec = AttendanceRecord(
            session_id=disc_req.session_id,
            student_id=disc_req.student_id,
            status='PRESENT',
            confidence=1.0,
            marked_by='TEACHER_APPROVAL',
            marked_at=datetime.utcnow()
        )
        db.session.add(rec)
    else:
        rec.status = 'PRESENT'
        rec.marked_at = datetime.utcnow()

    sess = disc_req.session
    if sess:
        leg_att = Attendance.query.filter_by(student_id=disc_req.student_id, date=sess.date, subject_id=sess.subject_id).first()
        if not leg_att:
            leg_att = Attendance(
                student_id=disc_req.student_id,
                date=sess.date,
                status='Present',
                subject_id=sess.subject_id,
                time_marked=get_current_time_str()
            )
            db.session.add(leg_att)
        else:
            leg_att.status = 'Present'

    db.session.commit()
    flash(f"Discrepancy request approved! {disc_req.student.name} marked as PRESENT.", "success")
    return redirect(url_for('teacher.dashboard'))

@teacher_bp.route('/teacher/reject_discrepancy/<int:req_id>', methods=['POST'])
@login_required
@teacher_required
def reject_discrepancy(req_id):
    disc_req = AttendanceDiscrepancyRequest.query.get_or_404(req_id)
    disc_req.status = 'REJECTED'
    db.session.commit()
    flash(f"Discrepancy request for {disc_req.student.name} rejected.", "info")
    return redirect(url_for('teacher.dashboard'))

@teacher_bp.route('/teacher/bulk_approve_discrepancies', methods=['POST'])
@login_required
@teacher_required
def bulk_approve_discrepancies():
    req_ids = request.form.getlist('discrepancy_ids')
    approved_count = 0
    for rid in req_ids:
        if str(rid).isdigit():
            disc_req = AttendanceDiscrepancyRequest.query.get(int(rid))
            if disc_req and disc_req.status == 'PENDING':
                disc_req.status = 'APPROVED'
                rec = AttendanceRecord.query.filter_by(session_id=disc_req.session_id, student_id=disc_req.student_id).first()
                if not rec:
                    rec = AttendanceRecord(
                        session_id=disc_req.session_id,
                        student_id=disc_req.student_id,
                        status='PRESENT',
                        confidence=1.0,
                        marked_by='TEACHER_APPROVAL',
                        marked_at=datetime.utcnow()
                    )
                    db.session.add(rec)
                else:
                    rec.status = 'PRESENT'
                    rec.marked_at = datetime.utcnow()
                
                sess = disc_req.session
                if sess:
                    leg_att = Attendance.query.filter_by(student_id=disc_req.student_id, date=sess.date, subject_id=sess.subject_id).first()
                    if not leg_att:
                        leg_att = Attendance(
                            student_id=disc_req.student_id,
                            date=sess.date,
                            status='Present',
                            subject_id=sess.subject_id,
                            time_marked=get_current_time_str()
                        )
                        db.session.add(leg_att)
                    else:
                        leg_att.status = 'Present'
                approved_count += 1
    db.session.commit()
    flash(f"Successfully approved {approved_count} student discrepancy request(s)!", "success")
    return redirect(url_for('teacher.dashboard'))

@teacher_bp.route('/teacher/student_modifications')
@login_required
@teacher_required
def student_modifications():
    teacher = get_current_teacher()
    
    teacher_subjects = Subject.query.filter_by(teacher_id=teacher.id).all()
    subject_ids = [s.id for s in teacher_subjects]
    
    teacher_sessions = AttendanceSession.query.filter(
        (AttendanceSession.teacher_id == teacher.id) |
        (AttendanceSession.subject_id.in_(subject_ids) if subject_ids else False)
    ).all()
    teacher_session_ids = [s.id for s in teacher_sessions]

    filter_conds = []
    if teacher_session_ids:
        filter_conds.append(AttendanceDiscrepancyRequest.session_id.in_(teacher_session_ids))
    if teacher and teacher.name:
        filter_conds.append(AttendanceDiscrepancyRequest.teacher_name.ilike(f"%{teacher.name}%"))

    if filter_conds:
        from sqlalchemy import or_
        mod_requests = AttendanceDiscrepancyRequest.query.filter(
            or_(*filter_conds)
        ).order_by(AttendanceDiscrepancyRequest.id.desc()).all()
    else:
        mod_requests = []

    return render_template(
        'teacher_student_modifications.html',
        mod_requests=mod_requests,
        teacher=teacher
    )

@teacher_bp.route('/teacher/quick_add_roll/<int:session_id>', methods=['POST'])
@login_required
@teacher_required
def quick_add_roll(session_id):
    session_rec = AttendanceSession.query.get_or_404(session_id)
    roll_no_input = request.form.get('roll_no', '').strip()
    
    if not roll_no_input:
        flash("Please enter a valid Roll Number.", "warning")
        return redirect(url_for('teacher.take_attendance', session_id=session_rec.id))
        
    roll_list = [r.strip() for r in roll_no_input.replace(',', ' ').split() if r.strip()]
    added_count = 0
    
    for roll in roll_list:
        student = Student.query.filter_by(class_id=session_rec.class_id, roll_no=roll).first()
        if not student:
            student = Student.query.filter_by(roll_no=roll).first()
            
        if student:
            rec = AttendanceRecord.query.filter_by(session_id=session_rec.id, student_id=student.id).first()
            if not rec:
                rec = AttendanceRecord(
                    session_id=session_rec.id,
                    student_id=student.id,
                    status='PRESENT',
                    confidence=1.0,
                    marked_by='MANUAL_ROLL',
                    marked_at=datetime.utcnow()
                )
                db.session.add(rec)
            else:
                rec.status = 'PRESENT'
                rec.marked_at = datetime.utcnow()
                
            leg_att = Attendance.query.filter_by(student_id=student.id, date=session_rec.date, subject_id=session_rec.subject_id).first()
            if not leg_att:
                leg_att = Attendance(
                    student_id=student.id,
                    date=session_rec.date,
                    status='Present',
                    subject_id=session_rec.subject_id,
                    time_marked=get_current_time_str()
                )
                db.session.add(leg_att)
            else:
                leg_att.status = 'Present'
            added_count += 1

    db.session.commit()
    if added_count > 0:
        flash(f"Successfully marked Roll Number(s) {', '.join(roll_list)} as PRESENT!", "success")
    else:
        flash(f"No matching student found for Roll Number(s) '{roll_no_input}'.", "danger")
        
        
    return redirect(url_for('teacher.take_attendance', session_id=session_rec.id))


@teacher_bp.route('/teacher/my_attendance')
@login_required
@teacher_required
def my_attendance():
    teacher = get_current_teacher()
    if not teacher:
        flash("Teacher profile not found.", "danger")
        return redirect(url_for('teacher.dashboard'))
    today = get_current_date()
    month_filter = request.args.get('month', '').strip() or today.strftime('%Y-%m')
    date_filter = request.args.get('date', '').strip()
    
    query = TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id)
    if date_filter:
        try:
            d_val = datetime.strptime(date_filter, '%Y-%m-%d').date()
            query = query.filter(TeacherDailyAttendance.attendance_date == d_val)
        except Exception:
            pass
    elif month_filter:
        try:
            yr, mo = map(int, month_filter.split('-'))
            start_d = date(yr, mo, 1)
            if mo == 12:
                end_d = date(yr + 1, 1, 1)
            else:
                end_d = date(yr, mo + 1, 1)
            query = query.filter(TeacherDailyAttendance.attendance_date >= start_d, TeacherDailyAttendance.attendance_date < end_d)
        except Exception:
            pass

    records = query.order_by(TeacherDailyAttendance.attendance_date.desc()).all()
    
    # Calculate statistics across all records
    all_recs = TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id).all()
    total_days = len(all_recs)
    present_days = sum(1 for r in all_recs if r.status == 'Present')
    half_days = sum(1 for r in all_recs if r.status == 'Half Day')
    absent_days = sum(1 for r in all_recs if r.status == 'Absent')
    leave_days = sum(1 for r in all_recs if r.status == 'Approved Leave')
    on_time_count = sum(1 for r in all_recs if r.late_status == 'On Time' and r.check_in_at is not None)
    late_count = sum(1 for r in all_recs if r.late_status == 'Late')

    effective_present = present_days + (0.5 * half_days)
    attendance_pct = round((effective_present / max(1, (total_days - leave_days))) * 100, 1) if total_days > 0 else 100.0

    return render_template(
        'teacher_my_attendance.html',
        teacher=teacher,
        records=records,
        month_filter=month_filter,
        total_days=total_days,
        present_days=present_days,
        half_days=half_days,
        absent_days=absent_days,
        leave_days=leave_days,
        on_time_count=on_time_count,
        late_count=late_count,
        attendance_pct=attendance_pct
    )


@teacher_bp.route('/teacher/announcements')
@login_required
@teacher_required
def announcements():
    teacher = get_current_teacher()
    if not teacher:
        flash("Teacher profile not found.", "danger")
        return redirect(url_for('teacher.dashboard'))

    # 1. Admin Notices for Teachers (target_role in 'TEACHERS', 'ALL') - excluding notices dismissed by this teacher
    dismissed_ids = [d.announcement_id for d in TeacherDismissedNotice.query.filter_by(teacher_id=teacher.id).all()]
    admin_notices_q = ClassAnnouncement.query.filter(
        ClassAnnouncement.posted_by_role == 'admin',
        ClassAnnouncement.target_role.in_(['TEACHERS', 'ALL'])
    )
    if dismissed_ids:
        admin_notices_q = admin_notices_q.filter(~ClassAnnouncement.id.in_(dismissed_ids))
    admin_notices = admin_notices_q.order_by(ClassAnnouncement.created_at.desc()).all()

    # Mark visible admin notices as read for this teacher so the badge clears
    read_notice_ids = {r.announcement_id for r in TeacherReadNotice.query.filter_by(teacher_id=teacher.id).all()}
    for ann in admin_notices:
        if ann.id not in read_notice_ids:
            db.session.add(TeacherReadNotice(teacher_id=teacher.id, announcement_id=ann.id))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 2. Announcements posted by this teacher (for students/classes)
    my_announcements = ClassAnnouncement.query.filter_by(
        teacher_id=teacher.id
    ).order_by(ClassAnnouncement.created_at.desc()).all()

    # Classes available to announce to
    classes = teacher.classes_directed if hasattr(teacher, 'classes_directed') and teacher.classes_directed else []
    all_classes = Class.query.order_by(Class.name).all()

    return render_template(
        'teacher_announcements.html',
        teacher=teacher,
        admin_notices=admin_notices,
        my_announcements=my_announcements,
        classes=classes or all_classes
    )


@teacher_bp.route('/teacher/announcement/create', methods=['POST'])
@login_required
@teacher_required
def create_announcement():
    teacher = get_current_teacher()
    if not teacher:
        flash("Teacher profile not found.", "danger")
        return redirect(url_for('teacher.announcements'))

    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    notice_type = request.form.get('notice_type', 'Announcement')
    class_id = request.form.get('class_id')

    if not title or not content:
        flash("Title and Content are required to publish an announcement.", "warning")
        return redirect(url_for('teacher.announcements'))

    ann = ClassAnnouncement(
        title=title,
        content=content,
        notice_type=notice_type,
        teacher_id=teacher.id,
        class_id=int(class_id) if class_id and class_id != 'ALL' else None,
        posted_by_role='teacher',
        target_role='STUDENTS',
        created_at=datetime.utcnow()
    )
    db.session.add(ann)
    db.session.commit()
    flash("✓ Announcement successfully published to students!", "success")
    return redirect(url_for('teacher.announcements'))


@teacher_bp.route('/teacher/announcement/delete/<int:ann_id>', methods=['POST'])
@login_required
@teacher_required
def delete_teacher_announcement(ann_id):
    teacher = get_current_teacher()
    ann = ClassAnnouncement.query.get_or_404(ann_id)
    
    # If teacher posted it, delete permanently from student feeds
    if ann.teacher_id == teacher.id or ann.posted_by_role == 'teacher':
        db.session.delete(ann)
        db.session.commit()
        flash("✓ Announcement deleted permanently from all student feeds.", "info")
    else:
        # If admin posted it, dismiss only for this teacher
        existing = TeacherDismissedNotice.query.filter_by(teacher_id=teacher.id, announcement_id=ann.id).first()
        if not existing:
            dismissed = TeacherDismissedNotice(teacher_id=teacher.id, announcement_id=ann.id)
            db.session.add(dismissed)
            db.session.commit()
        flash("✓ Notice deleted from your Notice Board.", "info")

    return redirect(url_for('teacher.announcements'))


@teacher_bp.route('/teacher/id_card')
@login_required
@teacher_required
def teacher_id_card():
    teacher = get_current_teacher()
    if not teacher:
        flash("Faculty profile not found.", "warning")
        return redirect(url_for('teacher.dashboard'))

    subjects = Subject.query.filter_by(teacher_id=teacher.id).all()
    assigned_classes = [s.class_assigned for s in subjects if s.class_assigned]
    
    # QR verification data payload
    qr_data = f"SMARTVISION:FACULTY|ID:{teacher.emp_id or teacher.id}|NAME:{teacher.name}|DEPT:{teacher.department or 'General'}|ROLE:TEACHER"
    
    return render_template(
        'teacher_id_card.html',
        teacher=teacher,
        subjects=subjects,
        assigned_classes=assigned_classes,
        qr_data=qr_data,
        today=date.today()
    )



