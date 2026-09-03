# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - FACULTY GEOFENCED ATTENDANCE MODULE
# ==============================================================================
# Description: Real-time geolocation verification (Haversine GPS boundary check),
#              facial recognition check-in/check-out, morning grace period management,
#              uninformed absence detection, and administrative audit logging.
# ==============================================================================

import math
from datetime import datetime, date, time, timedelta
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from extensions import db
from models import (
    User, Teacher, TeacherLeave, Holiday,
    TeacherOfficeLocation, TeacherAttendanceSettings,
    TeacherDailyAttendance, TeacherAttendanceAuditLog
)

teacher_attendance_bp = Blueprint('teacher_attendance', __name__)

# ==============================================================================
# SECTION 1: GEOLOCATION & HAVERSINE MATHEMATICAL HELPERS
# ==============================================================================
def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculates great-circle distance between two GPS coordinates in meters
    using the Haversine formula on Earth radius R = 6,371,000 meters.
    """
    R = 6371000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c  # Distance in meters

def get_current_ist_datetime():
    """Returns (now_dt: datetime, today_date: date) in Indian Standard Time (IST - UTC+5:30)."""
    try:
        from timezone_utils import get_ist_now
        ist_dt = get_ist_now()
        return ist_dt.replace(tzinfo=None), ist_dt.date()
    except Exception:
        now = datetime.now()
        return now, now.date()


def get_or_create_settings():
    settings = TeacherAttendanceSettings.query.first()
    if not settings:
        settings = TeacherAttendanceSettings(
            morning_start_time="08:00 AM",
            morning_deadline="09:00 AM",
            evening_start_time="04:00 PM",
            evening_end_time="07:00 PM",
            grace_period_mins=30,
            max_gps_accuracy_meters=50.0
        )
        db.session.add(settings)
        db.session.commit()
    return settings

def get_active_office_location():
    office = TeacherOfficeLocation.query.filter_by(status='Active').first()
    if not office:
        office = TeacherOfficeLocation.query.first()
    if not office:
        office = TeacherOfficeLocation(
            name="Admin Office",
            latitude=22.2887,
            longitude=73.3634,
            allowed_radius=50.0,
            status='Active'
        )
        db.session.add(office)
        db.session.commit()
    return office

def parse_time_str(time_str):
    """Parse time string like '08:00 AM' or '17:00' to time object."""
    if not time_str:
        return None
    for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p"):
        try:
            return datetime.strptime(time_str.strip(), fmt).time()
        except ValueError:
            pass
    return None

def recalculate_daily_status(rec, settings=None):
    """
    Calculate daily status (Present, Half Day, Absent, Approved Leave, Holiday, Weekend, Pending).
    Enforces:
    - 8:00 AM - 9:00 AM: Present (On Time)
    - 9:00 AM - 9:30 AM (30-min late allowance): Present (Late) with exact minutes
    - After 9:30 AM Cutoff without Check-In: Auto-marked Absent
      - If leave applied / informed admin: Informed Absent / Approved Leave
      - If no prior info: Flagged as Uninformed Absent
    """
    if not rec:
        return 'Pending'

    # 1. Admin Override (Highest Priority for manual corrections)
    if rec.is_admin_overridden:
        return rec.status

    if not settings:
        settings = get_or_create_settings()

    # 2. Approved / Applied Leave Check
    leave = TeacherLeave.query.filter(
        TeacherLeave.teacher_id == rec.teacher_id,
        TeacherLeave.status.in_(['APPROVED', 'Approved', 'PENDING', 'Pending']),
        TeacherLeave.date_from <= rec.attendance_date,
        TeacherLeave.date_to >= rec.attendance_date
    ).first()
    
    if leave and leave.status.upper() == 'APPROVED':
        rec.status = 'Approved Leave'
        rec.is_uninformed_absence = False
        return rec.status

    # 3. Holiday Check (Only Global/ALL scope holidays apply to faculty; CLASSES_ONLY/STUDENTS_ONLY remain working days)
    holiday = Holiday.query.filter(
        Holiday.date == rec.attendance_date,
        (Holiday.scope == 'ALL') | (Holiday.scope == 'all')
    ).first()
    if holiday:
        rec.status = 'Holiday'
        rec.is_uninformed_absence = False
        return rec.status

    # 4. Weekend Check (Sunday)
    if rec.attendance_date.weekday() == 6: # Sunday
        rec.status = 'Weekend'
        rec.is_uninformed_absence = False
        return rec.status

    has_checkin = rec.check_in_at is not None
    has_checkout = rec.check_out_at is not None

    # 5. Physical presence check
    if has_checkin:
        rec.is_uninformed_absence = False
        # Calculate late minutes if checked in after morning deadline (e.g. 09:00 AM)
        morn_deadline = parse_time_str(settings.morning_deadline) or time(9, 0)
        checkin_time = rec.check_in_at.time()
        if checkin_time > morn_deadline:
            rec.late_status = 'Late'
            deadline_dt = datetime.combine(rec.attendance_date, morn_deadline)
            rec.late_minutes = max(1, int((rec.check_in_at - deadline_dt).total_seconds() / 60))
        else:
            rec.late_status = 'On Time'
            rec.late_minutes = 0

        now, today = get_current_ist_datetime()
        evening_end_time = parse_time_str(settings.evening_end_time) or time(17, 0)
        eve_cutoff_dt = datetime.combine(today, evening_end_time) + timedelta(minutes=30)
        eve_cutoff_time = eve_cutoff_dt.time()

        if has_checkout:
            rec.status = 'Present'
        else:
            if rec.attendance_date == today and now.time() <= eve_cutoff_time:
                rec.status = 'Present'
            else:
                rec.status = 'Half Day'
        return rec.status

    if has_checkout and not has_checkin:
        rec.status = 'Half Day'
        rec.is_uninformed_absence = False
        return rec.status

    # Neither marked
    now, today = get_current_ist_datetime()
    morn_deadline = parse_time_str(settings.morning_deadline) or time(9, 0)
    grace_mins = settings.grace_period_mins if settings.grace_period_mins is not None else 30
    cutoff_dt = datetime.combine(today, morn_deadline) + timedelta(minutes=grace_mins)
    cutoff_time = cutoff_dt.time()

    if rec.attendance_date < today or (rec.attendance_date == today and now.time() > cutoff_time):
        rec.status = 'Absent'
        if leave: # Prior leave application submitted
            rec.informed_admin = True
            rec.absence_reason = f"Leave application: {leave.reason or 'Personal/Medical'}"
            rec.is_uninformed_absence = False
            rec.late_status = 'Leave Applied'
        elif rec.informed_admin: # Admin was previously informed
            rec.is_uninformed_absence = False
            rec.late_status = 'Informed Absent'
        else: # No prior notice or leave application
            rec.is_uninformed_absence = True
            rec.late_status = 'Uninformed Absent'
    else:
        rec.status = 'Pending'
        rec.is_uninformed_absence = False

    return rec.status


def verify_teacher_face(teacher, captured_base64, session_name='checkin'):
    """
    Verifies that the camera photo contains an authentic face,
    and matches against the teacher's registered face profile.
    Saves the audit photo in uploads/faces/.
    Returns (success: bool, message: str, filename: str or None, is_face_verified: bool).
    """
    if not captured_base64 or not str(captured_base64).startswith('data:image/'):
        return False, "Live camera face capture is required to mark attendance.", None, False

    try:
        import os
        import io
        import base64
        import numpy as np
        from PIL import Image
        from werkzeug.utils import secure_filename

        format_part, imgstr = captured_base64.split(';base64,', 1)
        ext = 'jpg'
        if 'png' in format_part:
            ext = 'png'

        image_bytes = base64.b64decode(imgstr)
        img_pil = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        img_np = np.array(img_pil)

        # Save photo to uploads/faces/ for attendance audit record
        upload_folder = os.path.join(os.getcwd(), 'uploads', 'faces')
        os.makedirs(upload_folder, exist_ok=True)
        today_str = date.today().strftime('%Y%m%d')
        safe_name = secure_filename((teacher.name or 'teacher').replace(' ', '_'))
        filename = f"teacher_{teacher.id}_{safe_name}_{session_name}_{today_str}_{int(datetime.now().timestamp())}.{ext}"
        filepath = os.path.join(upload_folder, filename)
        img_pil.save(filepath, quality=90)

        # 1. Recover teacher face encoding if not present in DB
        if not teacher.face_encoding and (teacher.image_filename or teacher.image_data):
            try:
                from face_detector_engine import get_face_biometrics_robust
                if teacher.image_filename:
                    prof_path = os.path.join(upload_folder, teacher.image_filename)
                    if not os.path.exists(prof_path):
                        prof_path = os.path.join(os.getcwd(), 'static', 'faces', teacher.image_filename)
                    if os.path.exists(prof_path):
                        ref_pil = Image.open(prof_path).convert('RGB')
                        _, ref_encs = get_face_biometrics_robust(np.array(ref_pil))
                        if ref_encs and ref_encs[0] is not None:
                            teacher.face_encoding = ref_encs[0].tobytes()
                            db.session.commit()
            except Exception as rec_err:
                print(f"[Teacher Encoding Recovery Error]: {rec_err}")

        # 2. Detect face and extract 128-d embeddings using robust multi-pass engine
        from face_detector_engine import get_face_biometrics_robust, match_face_encoding
        face_locations, unknown_encodings = get_face_biometrics_robust(img_np)
        valid_encodings = [e for e in unknown_encodings if e is not None]

        # 3. Check face presence & single-face constraint
        if len(face_locations) == 0 or len(valid_encodings) == 0:
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except Exception: pass
            return False, "No face detected in camera photo. Please align your face clearly inside the scanner oval.", None, False

        if len(face_locations) > 1:
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except Exception: pass
            return False, f"Multiple faces ({len(face_locations)}) detected in camera view. Please ensure only you are in front of the camera.", None, False

        # 4. Strictly match against registered face encoding
        if teacher.face_encoding and len(valid_encodings) > 0:
            try:
                known_encoding = np.frombuffer(teacher.face_encoding, dtype=np.float64)
                best_idx, min_dist, is_match, conf_str = match_face_encoding(valid_encodings[0], [known_encoding], tolerance=0.60)
                if not is_match:
                    if os.path.exists(filepath):
                        try: os.remove(filepath)
                        except Exception: pass
                    return False, f"Face verification failed: Face does not match registered profile (Match Distance: {round(min_dist, 2)}).", None, False
            except Exception as e:
                print(f"[Face Match Check Warning]: {e}")
        elif not teacher.face_encoding and len(valid_encodings) > 0:
            # First-time automatic enrollment from verified check-in
            teacher.face_encoding = valid_encodings[0].tobytes()
            db.session.commit()

        return True, "✓ Face & Geolocation Verified!", filename, True

    except Exception as e:
        print(f"[verify_teacher_face Exception]: {e}")
        return True, "Geolocation verified.", None, False

@teacher_attendance_bp.route('/teacher_attendance/status', methods=['GET'])
@login_required
def get_teacher_daily_status():
    """Return JSON status of today's attendance for the logged-in teacher."""
    if current_user.role != 'teacher' or not current_user.teacher_profile:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    teacher = current_user.teacher_profile

    # Strict Suspension & Campus Access Restriction Check
    if teacher.is_suspended:
        reason_text = teacher.custom_suspension_reason or teacher.suspension_reason or "Disciplinary / Administrative Issue"
        return jsonify({
            'success': False,
            'is_suspended': True,
            'message': f"🔒 ACCESS DENIED: Your Faculty ID Card is currently SUSPENDED.\n\nReason: {reason_text}\n\nYou cannot mark attendance while your ID card is suspended. Please meet the Admin Office or submit a suspension-removal request."
        }), 403

    now, today = get_current_ist_datetime()
    office = get_active_office_location()
    settings = get_or_create_settings()

    rec = TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id, attendance_date=today).first()
    if rec:
        recalculate_daily_status(rec, settings)
        db.session.commit()

    return jsonify({
        'success': True,
        'date': today.strftime('%B %d, %Y'),
        'office': {
            'id': office.id,
            'name': office.name,
            'allowed_radius': office.allowed_radius,
            'latitude': office.latitude,
            'longitude': office.longitude
        },
        'settings': {
            'morning_start': settings.morning_start_time,
            'morning_deadline': settings.morning_deadline,
            'evening_start': settings.evening_start_time,
            'evening_end': settings.evening_end_time,
            'grace_period_mins': settings.grace_period_mins,
            'max_gps_accuracy': settings.max_gps_accuracy_meters
        },
        'attendance': {
            'status': rec.status if rec else 'Pending',
            'late_status': rec.late_status if rec else 'On Time',
            'check_in_at': rec.check_in_at.strftime('%I:%M %p') if (rec and rec.check_in_at) else None,
            'check_in_distance': round(rec.check_in_distance, 1) if (rec and rec.check_in_distance is not None) else None,
            'check_in_face_verified': bool(rec.check_in_face_verified) if rec else False,
            'check_out_at': rec.check_out_at.strftime('%I:%M %p') if (rec and rec.check_out_at) else None,
            'check_out_distance': round(rec.check_out_distance, 1) if (rec and rec.check_out_distance is not None) else None,
            'check_out_face_verified': bool(rec.check_out_face_verified) if rec else False,
        }
    })


@teacher_attendance_bp.route('/teacher_attendance/mark_morning', methods=['POST'])
@login_required
def mark_morning_attendance():
    """Process Morning Check-In with Geolocation and Face Recognition validation."""
    if current_user.role != 'teacher' or not current_user.teacher_profile:
        return jsonify({'success': False, 'message': 'Only registered teachers can mark attendance.'}), 403

    data = request.get_json() or {}
    try:
        lat = float(data.get('latitude'))
        lon = float(data.get('longitude'))
        accuracy = float(data.get('accuracy', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Invalid GPS coordinates received.'}), 400

    captured_image_base64 = data.get('captured_image_base64')
    teacher = current_user.teacher_profile

    # Strict Suspension & Campus Access Restriction Check
    if teacher.is_suspended:
        reason_text = teacher.custom_suspension_reason or teacher.suspension_reason or "Disciplinary / Administrative Issue"
        return jsonify({
            'success': False,
            'is_suspended': True,
            'message': f"🔒 ACCESS DENIED: Your Faculty ID Card is currently SUSPENDED.\n\nReason: {reason_text}\n\nYou cannot mark attendance while your ID card is suspended. Please meet the Admin Office or submit a suspension-removal request."
        }), 403

    now, today = get_current_ist_datetime()
    office = get_active_office_location()
    settings = get_or_create_settings()

    # 1. Timing Window Check (8:00 AM - 9:00 AM on time, 9:00 AM - 9:30 AM 30-min late allowance)
    morn_start = parse_time_str(settings.morning_start_time) or time(8, 0)
    morn_deadline = parse_time_str(settings.morning_deadline) or time(9, 0)
    grace_mins = settings.grace_period_mins if settings.grace_period_mins is not None else 30
    grace_cutoff = (datetime.combine(today, morn_deadline) + timedelta(minutes=grace_mins)).time()

    if now.time() < morn_start:
        return jsonify({
            'success': False,
            'message': f'Morning Check-In is not active yet. Allowed window starts at {settings.morning_start_time}.'
        }), 400

    if now.time() > grace_cutoff:
        return jsonify({
            'success': False,
            'message': f"❌ Morning Check-In is closed. Allowed window was {settings.morning_start_time} - {grace_cutoff.strftime('%I:%M %p')} (including 30-minute late grace period). Your status has been auto-marked as Absent."
        }), 400

    # 2. Accuracy Check
    if accuracy > settings.max_gps_accuracy_meters:
        return jsonify({
            'success': False,
            'message': f"GPS signal accuracy is too weak ({round(accuracy, 1)}m). Please enable location services or move to an open area and try again."
        }), 400

    # 3. Haversine Distance Verification
    dist = haversine_distance(lat, lon, office.latitude, office.longitude)
    if dist > office.allowed_radius:
        return jsonify({
            'success': False,
            'message': f'Location Verification Failed. You are currently outside the allowed office area ({round(dist, 1)}m away).',
            'distance': round(dist, 1),
            'allowed_radius': office.allowed_radius,
            'office_name': office.name
        }), 400

    # 3. Retrieve or Create Daily Record
    rec = TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id, attendance_date=today).first()
    if not rec:
        rec = TeacherDailyAttendance(teacher_id=teacher.id, attendance_date=today)
        db.session.add(rec)

    if rec.check_in_at is not None:
        return jsonify({
            'success': False,
            'message': f"Morning Attendance already checked in at {rec.check_in_at.strftime('%I:%M %p')}."
        }), 400

    # 4. Face Recognition Verification
    face_ok, face_msg, face_filename, is_face_verified = verify_teacher_face(teacher, captured_image_base64, session_name='morning')
    if not face_ok:
        return jsonify({
            'success': False,
            'message': face_msg,
            'distance': round(dist, 1)
        }), 400

    # 5. Check Late Status (On Time within morning deadline, Late if between deadline and 1-hour grace cutoff)
    rec.check_in_at = now
    rec.check_in_latitude = lat
    rec.check_in_longitude = lon
    rec.check_in_accuracy = accuracy
    rec.check_in_distance = dist
    rec.check_in_face_verified = is_face_verified
    if face_filename:
        rec.check_in_photo = face_filename
    rec.office_location_id = office.id
    rec.is_admin_overridden = False

    if now.time() > morn_deadline:
        rec.late_status = 'Late'
        deadline_dt = datetime.combine(today, morn_deadline)
        rec.late_minutes = max(1, int((now - deadline_dt).total_seconds() / 60))
    else:
        rec.late_status = 'On Time'
        rec.late_minutes = 0

    recalculate_daily_status(rec, settings)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f"✓ Morning Attendance & Face Verified Successfully! ({'Present - Late' if rec.late_status == 'Late' else 'Present - On Time'})",
        'check_in_time': now.strftime('%I:%M %p'),
        'distance': round(dist, 1),
        'face_verified': is_face_verified,
        'status': rec.status,
        'late_status': rec.late_status
    })


@teacher_attendance_bp.route('/teacher_attendance/mark_evening', methods=['POST'])
@login_required
def mark_evening_attendance():
    """Process Evening Check-Out with Geolocation and Face Recognition validation."""
    if current_user.role != 'teacher' or not current_user.teacher_profile:
        return jsonify({'success': False, 'message': 'Only registered teachers can mark attendance.'}), 403

    teacher = current_user.teacher_profile
    if teacher.is_suspended:
        reason_text = teacher.custom_suspension_reason or teacher.suspension_reason or "Disciplinary / Administrative Issue"
        return jsonify({
            'success': False,
            'is_suspended': True,
            'message': f"🔒 ACCESS DENIED: Your Faculty ID Card is currently SUSPENDED.\n\nReason: {reason_text}"
        }), 403

    data = request.get_json() or {}
    try:
        lat = float(data.get('latitude'))
        lon = float(data.get('longitude'))
        accuracy = float(data.get('accuracy', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Invalid GPS coordinates received.'}), 400

    captured_image_base64 = data.get('captured_image_base64')
    teacher = current_user.teacher_profile
    now, today = get_current_ist_datetime()
    office = get_active_office_location()
    settings = get_or_create_settings()

    # 1. Timing Window Check (Restricted before start and after 30-min relaxation cutoff)
    eve_start = parse_time_str(settings.evening_start_time) or time(16, 0)
    eve_end = parse_time_str(settings.evening_end_time) or time(17, 0)
    eve_cutoff_dt = datetime.combine(today, eve_end) + timedelta(minutes=30)
    eve_cutoff = eve_cutoff_dt.time()

    if now.time() < eve_start:
        return jsonify({
            'success': False,
            'message': f'Evening Check-Out is restricted before time. Allowed window starts at {settings.evening_start_time}.'
        }), 400

    if now.time() > eve_cutoff:
        # Check-out window locked: ensure daily status is marked Half Day
        rec = TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id, attendance_date=today).first()
        if rec:
            recalculate_daily_status(rec, settings)
            db.session.commit()
        return jsonify({
            'success': False,
            'message': f"❌ Evening Check-Out is locked. Allowed window was {settings.evening_start_time} - {settings.evening_end_time} (+30m relaxation until {eve_cutoff.strftime('%I:%M %p')}). Attendance recorded as Half Day."
        }), 400

    # 2. Accuracy Check
    if accuracy > settings.max_gps_accuracy_meters:
        return jsonify({
            'success': False,
            'message': f"GPS signal accuracy is too weak ({round(accuracy, 1)}m). Please enable location services or move to an open area and try again."
        }), 400

    # 3. Distance Check
    dist = haversine_distance(lat, lon, office.latitude, office.longitude)
    if dist > office.allowed_radius:
        return jsonify({
            'success': False,
            'message': f'Location Verification Failed. You are currently outside the allowed office area ({round(dist, 1)}m away).',
            'distance': round(dist, 1),
            'allowed_radius': office.allowed_radius,
            'office_name': office.name
        }), 400

    # 3. Retrieve or Create Daily Record
    rec = TeacherDailyAttendance.query.filter_by(teacher_id=teacher.id, attendance_date=today).first()
    if not rec:
        rec = TeacherDailyAttendance(teacher_id=teacher.id, attendance_date=today)
        db.session.add(rec)

    if rec.check_out_at is not None:
        return jsonify({
            'success': False,
            'message': f"Evening Attendance already checked out at {rec.check_out_at.strftime('%I:%M %p')}."
        }), 400

    # 4. Face Recognition Verification
    face_ok, face_msg, face_filename, is_face_verified = verify_teacher_face(teacher, captured_image_base64, session_name='evening')
    if not face_ok:
        return jsonify({
            'success': False,
            'message': face_msg,
            'distance': round(dist, 1)
        }), 400

    rec.check_out_at = now
    rec.check_out_latitude = lat
    rec.check_out_longitude = lon
    rec.check_out_accuracy = accuracy
    rec.check_out_distance = dist
    rec.check_out_face_verified = is_face_verified
    if face_filename:
        rec.check_out_photo = face_filename
    rec.office_location_id = office.id
    rec.is_admin_overridden = False

    recalculate_daily_status(rec, settings)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': '✓ Evening Attendance & Face Verified Successfully!',
        'check_out_time': now.strftime('%I:%M %p'),
        'distance': round(dist, 1),
        'face_verified': is_face_verified,
        'status': rec.status
    })


# --- ADMIN TEACHER ATTENDANCE MANAGEMENT ROUTES ---

@teacher_attendance_bp.route('/admin/teacher_attendance', methods=['GET'])
@login_required
def admin_teacher_attendance():
    """Admin Dashboard for Teacher Attendance Logs, Office Location Geofence & Timing Settings."""
    if current_user.role != 'admin':
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))

    now, today = get_current_ist_datetime()
    selected_date_str = request.args.get('date', today.strftime('%Y-%m-%d'))
    try:
        selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
    except ValueError:
        selected_date = today

    selected_department = request.args.get('department', '').strip()
    selected_teacher_id = request.args.get('teacher_id', type=int)
    selected_status = request.args.get('status', '').strip()

    from models import Department
    all_departments = Department.query.order_by(Department.name.asc()).all()

    office = get_active_office_location()
    settings = get_or_create_settings()
    teachers = Teacher.query.order_by(Teacher.name).all()

    if not all_departments:
        dept_names = sorted(list(set(t.department for t in teachers if t.department)))
        all_departments = [type('DeptObj', (object,), {'name': d, 'code': d})() for d in dept_names]

    # Ensure all teachers have a daily attendance record for the selected date
    for t in teachers:
        rec = TeacherDailyAttendance.query.filter_by(teacher_id=t.id, attendance_date=selected_date).first()
        if not rec:
            rec = TeacherDailyAttendance(teacher_id=t.id, attendance_date=selected_date)
            db.session.add(rec)
    db.session.commit()

    # Query filtered logs
    query = TeacherDailyAttendance.query.filter_by(attendance_date=selected_date)
    if selected_teacher_id:
        query = query.filter_by(teacher_id=selected_teacher_id)

    records = query.all()

    # Recalculate status for records
    present_count = 0
    half_day_count = 0
    absent_count = 0
    leave_count = 0

    for r in records:
        st = recalculate_daily_status(r, settings)
        if st == 'Present':
            present_count += 1
        elif st == 'Half Day':
            half_day_count += 1
        elif st == 'Absent':
            absent_count += 1
        elif st in ('Approved Leave', 'Official Duty'):
            leave_count += 1

    db.session.commit()

    # Filter by department if specified
    if selected_department:
        records = [r for r in records if r.teacher and r.teacher.department and r.teacher.department.lower() == selected_department.lower()]

    if selected_status:
        records = [r for r in records if r.status == selected_status]

    audit_logs = TeacherAttendanceAuditLog.query.filter_by(attendance_date=selected_date).order_by(TeacherAttendanceAuditLog.timestamp.desc()).all()

    return render_template(
        'admin_teacher_attendance.html',
        selected_date=selected_date.strftime('%Y-%m-%d'),
        selected_department=selected_department,
        all_departments=all_departments,
        selected_teacher_id=selected_teacher_id,
        selected_status=selected_status,
        records=records,
        teachers=teachers,
        office=office,
        settings=settings,
        present_count=present_count,
        half_day_count=half_day_count,
        absent_count=absent_count,
        leave_count=leave_count,
        total_teachers=len(teachers),
        audit_logs=audit_logs
    )


@teacher_attendance_bp.route('/admin/teacher_attendance/settings/location', methods=['POST'])
@login_required
def update_office_location():
    """Save Admin Office Location & Geofence Radius."""
    if current_user.role != 'admin':
        flash('Unauthorized action.', 'danger')
        return redirect(url_for('main.dashboard'))

    name = request.form.get('name', 'Admin Office').strip()
    try:
        latitude = float(request.form.get('latitude'))
        longitude = float(request.form.get('longitude'))
        allowed_radius = float(request.form.get('allowed_radius', 50.0))
    except (ValueError, TypeError):
        flash('Invalid latitude, longitude, or radius value.', 'danger')
        return redirect(url_for('teacher_attendance.admin_teacher_attendance'))

    status = request.form.get('status', 'Active')

    office = TeacherOfficeLocation.query.filter_by(status='Active').first()
    if not office:
        office = TeacherOfficeLocation()
        db.session.add(office)

    office.name = name
    office.latitude = latitude
    office.longitude = longitude
    office.allowed_radius = allowed_radius
    office.status = status
    office.updated_at = datetime.utcnow()

    db.session.commit()
    flash(f'Office Attendance Location updated to "{name}" ({allowed_radius}m geofence radius).', 'success')
    return redirect(url_for('teacher_attendance.admin_teacher_attendance'))


@teacher_attendance_bp.route('/admin/teacher_attendance/settings/times', methods=['POST'])
@login_required
def update_attendance_times():
    """Save Admin Attendance Timing Rules."""
    if current_user.role != 'admin':
        flash('Unauthorized action.', 'danger')
        return redirect(url_for('main.dashboard'))

    morning_start_time = request.form.get('morning_start_time', '08:00 AM').strip()
    morning_deadline = request.form.get('morning_deadline', '11:00 AM').strip()
    evening_start_time = request.form.get('evening_start_time', '04:00 PM').strip()
    evening_end_time = request.form.get('evening_end_time', '07:00 PM').strip()
    
    try:
        grace_period_mins = int(request.form.get('grace_period_mins', 15))
        max_gps_accuracy_meters = float(request.form.get('max_gps_accuracy_meters', 50.0))
    except ValueError:
        flash('Invalid grace period or accuracy values.', 'danger')
        return redirect(url_for('teacher_attendance.admin_teacher_attendance'))

    settings = get_or_create_settings()
    settings.morning_start_time = morning_start_time
    settings.morning_deadline = morning_deadline
    settings.evening_start_time = evening_start_time
    settings.evening_end_time = evening_end_time
    settings.grace_period_mins = grace_period_mins
    settings.max_gps_accuracy_meters = max_gps_accuracy_meters
    settings.updated_at = datetime.utcnow()

    db.session.commit()
    flash('Teacher Attendance Timing and Accuracy rules updated successfully.', 'success')
    return redirect(url_for('teacher_attendance.admin_teacher_attendance'))


@teacher_attendance_bp.route('/admin/teacher_attendance/override', methods=['POST'])
@login_required
def override_teacher_attendance():
    """Admin Override of Teacher Daily Attendance status with Rationale Audit Logging."""
    if current_user.role != 'admin':
        flash('Unauthorized action.', 'danger')
        return redirect(url_for('main.dashboard'))

    record_id = request.form.get('record_id', type=int)
    new_status = request.form.get('new_status', '').strip() if request.form.get('new_status') else ''
    rationale = request.form.get('rationale', '').strip() if request.form.get('rationale') else ''

    if not record_id or not new_status or not rationale:
        flash('Please select a valid status and provide a rationale for the override.', 'warning')
        return redirect(url_for('teacher_attendance.admin_teacher_attendance'))

    rec = TeacherDailyAttendance.query.get_or_404(record_id)
    prev_status = rec.status

    rec.status = new_status
    rec.is_admin_overridden = True
    rec.updated_at = datetime.utcnow()

    # Create Audit Log Entry
    audit = TeacherAttendanceAuditLog(
        teacher_daily_attendance_id=rec.id,
        teacher_id=rec.teacher_id,
        attendance_date=rec.attendance_date,
        action='ADMIN_OVERRIDE',
        previous_status=prev_status,
        new_status=new_status,
        changed_by_user_id=current_user.id,
        rationale=rationale,
        timestamp=datetime.utcnow()
    )
    db.session.add(audit)
    db.session.commit()

    try:
        from schedule_service import generate_daily_schedule
        generate_daily_schedule(rec.attendance_date)
    except Exception as sched_err:
        print(f"[Attendance Override] Error regenerating schedule: {sched_err}")

    flash(f'Attendance status for {rec.teacher.name} updated to "{new_status}". Audit log recorded.', 'success')
    return redirect(url_for('teacher_attendance.admin_teacher_attendance', date=rec.attendance_date.strftime('%Y-%m-%d')))
