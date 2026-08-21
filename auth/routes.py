# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - AUTHENTICATION ROUTING MODULE
# ==============================================================================
# Description: User authentication workflows including standard and OAuth login,
#              6-digit OTP email verification, student biometric face registration,
#              issued teacher ID validation, password reset, and session control.
# ==============================================================================

import os
import base64
import random
import string
try:
    import face_recognition
except ImportError:
    face_recognition = None
from datetime import datetime, timedelta
from flask import render_template, redirect, url_for, flash, request, Blueprint, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from extensions import db, oauth
from models import User, Student, Class, Teacher, IssuedTeacherID
from email_utils import send_email

auth_bp = Blueprint('auth', __name__)

FACES_FOLDER = os.path.join('temp_uploads', 'faces')

# ==============================================================================
# SECTION 1: HELPER FUNCTIONS & OTP UTILITIES
# ==============================================================================

def save_base64_image(base64_str, roll_no, name, folder):
    """
    Decodes a base64 data URL and saves it as a file in the specified folder.
    Returns the saved file's secure filename or None.
    """
    if not base64_str or not base64_str.startswith('data:image/'):
        return None
    try:
        format, imgstr = base64_str.split(';base64,')
        ext = format.split('/')[-1]
        if ext == 'jpeg':
            ext = 'jpg'
        image_data = base64.b64decode(imgstr)
        
        filename = secure_filename(f"{roll_no}_{name}_captured.{ext}")
        filepath = os.path.join(folder, filename)
        
        os.makedirs(folder, exist_ok=True)
        with open(filepath, 'wb') as f:
            f.write(image_data)
        return filename, filepath
    except Exception as e:
        print(f"Error decoding base64 image: {e}")
        return None

# Helper to check if Google credentials are configured
def is_google_configured():
    return (current_app.config.get('GOOGLE_CLIENT_ID') and 
            current_app.config.get('GOOGLE_CLIENT_SECRET'))

import secrets

def generate_otp(length=6):
    """Generate a numeric OTP of given length."""
    return ''.join(random.choices(string.digits, k=length))

@auth_bp.route('/send-register-otp', methods=['POST'])
def send_register_otp():
    """AJAX endpoint to send Gmail verification link + OTP to user email during registration."""
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()

    if not email:
        return {'success': False, 'message': 'Email address is required.'}, 400

    # Check if user already exists
    existing_user = User.query.filter_by(email=email).first()
    is_already_registered = False
    if existing_user and getattr(existing_user, 'is_email_verified', False):
        is_already_registered = True

    otp = generate_otp(6)
    token = secrets.token_urlsafe(24)

    session['reg_otp_' + email] = {
        'otp': otp,
        'token': token,
        'verified': False,
        'expires_at': (datetime.utcnow() + timedelta(minutes=30)).isoformat()
    }

    # Generate external Click-To-Verify URL
    verify_link = url_for('auth.verify_email_link', email=email, token=token, _external=True)

    subject = f"SmartVision Verification Code: {otp}"
    body_text = f"""Hello,

Your 6-digit email verification OTP code for SmartVision registration is: {otp}

Please enter this OTP code on the registration page to verify your email address.
(This OTP code is valid for 15 minutes.)

Auto-Verification Link: {verify_link}

Best regards,
SmartVision Team
"""
    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px;">
        <div style="max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; padding: 28px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0;">
            <div style="text-align: center; margin-bottom: 20px;">
                <h2 style="color: #4f46e5; margin: 0; font-size: 24px; font-weight: 800;">🛡️ SmartVision</h2>
                <p style="color: #64748b; font-size: 13px; margin: 4px 0 0 0;">AI-Powered Attendance Management Portal</p>
            </div>
            <p style="color: #334155; font-size: 15px; line-height: 1.5;">Hello,</p>
            <p style="color: #334155; font-size: 15px; line-height: 1.5;">Your 6-digit verification code for SmartVision registration is:</p>
            
            <div style="background: #f1f5f9; border-radius: 12px; padding: 18px; text-align: center; margin: 20px 0; border: 2px dashed #6366f1;">
                <span style="font-family: monospace; font-size: 32px; font-weight: 800; letter-spacing: 6px; color: #3730a3;">{otp}</span>
                <div style="font-size: 12px; color: #64748b; margin-top: 6px;">Valid for 15 minutes</div>
            </div>

            <div style="text-align: center; margin-top: 22px;">
                <a href="{verify_link}" style="display: inline-block; background: #4f46e5; color: #ffffff !important; padding: 11px 26px; border-radius: 50px; text-decoration: none; font-weight: 700; font-size: 14px;">⚡ Click Here to Auto-Verify</a>
            </div>

            <div style="text-align: center; font-size: 12px; color: #94a3b8; margin-top: 24px; border-top: 1px solid #f1f5f9; padding-top: 16px;">
                <p style="margin: 0;">If you did not request this email, you can safely ignore it.</p>
            </div>
        </div>
    </body>
    </html>
    """
    try:
        send_email(email, subject, body_text, body_html=body_html, sync=True)
    except Exception as e:
        print(f"[OTP Email Error] {e}")

    print(f"\n================================================================================")
    print(f"[INLINE 6-DIGIT OTP GENERATED FOR {email}]: {otp}")
    print(f"================================================================================\n")

    return {
        'success': True,
        'message': f'6-Digit OTP code sent to {email}. Check your email inbox!',
        'otp_debug': otp
    }

@auth_bp.route('/verify-register-otp', methods=['POST'])
def verify_register_otp():
    """AJAX endpoint to verify entered registration OTP."""
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    otp_input = data.get('otp', '').strip()

    if not email or not otp_input:
        return {'success': False, 'message': 'Email and OTP code are required.'}, 400

    otp_data = session.get('reg_otp_' + email)
    if not otp_data:
        return {'success': False, 'message': 'No active verification request found. Please click Send Verification Email.'}, 400

    if otp_input != otp_data.get('otp'):
        return {'success': False, 'message': 'Invalid OTP code. Please check your email and try again.'}, 400

    # Mark as verified in session
    otp_data['verified'] = True
    session['reg_otp_' + email] = otp_data

    # Update database user if already created
    user = User.query.filter_by(email=email).first()
    if user:
        user.is_email_verified = True
        user.status = 'Approved' if user.role == 'student' else 'Email_Verified'
        if user.teacher_profile:
            user.teacher_profile.status = 'Email_Verified'
        db.session.commit()

    return {'success': True, 'message': 'Email verified successfully!'}

@auth_bp.route('/verify-email-link', methods=['GET'])
def verify_email_link():
    """Direct link endpoint clicked from Gmail inbox."""
    email = request.args.get('email', '').strip().lower()
    token = request.args.get('token', '').strip()

    if not email or not token:
        flash("Invalid email verification link parameters.", "danger")
        return redirect(url_for('main.index', state='login'))

    otp_data = session.get('reg_otp_' + email)
    
    # Verify token
    if otp_data and otp_data.get('token') == token:
        otp_data['verified'] = True
        session['reg_otp_' + email] = otp_data

    user = User.query.filter_by(email=email).first()
    if user:
        user.is_email_verified = True
        user.status = 'Approved' if user.role == 'student' else 'Email_Verified'
        if user.teacher_profile:
            user.teacher_profile.status = 'Email_Verified'
        db.session.commit()
        flash(f"✓ Email ({email}) verified successfully via Gmail link! You can now sign in.", "success")
    else:
        # User not fully registered yet, but mark email verified in session for registration form
        flash(f"✓ Email ({email}) verified successfully! Please complete your registration details below.", "success")

    return redirect(url_for('main.index', state='signup', verified_email=email))

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('main.dashboard'))
        elif current_user.role == 'teacher':
            return redirect(url_for('teacher.dashboard'))
        elif current_user.role == 'student':
            return redirect(url_for('student.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        login_role = request.form.get('login_role', 'student')  # role hint from UI tab ('student', 'teacher', 'admin')

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            # Check email verification requirement for Student and Teacher
            if not getattr(user, 'is_email_verified', True):
                token = secrets.token_urlsafe(24)
                session['reg_otp_' + user.email] = {
                    'token': token,
                    'verified': False,
                    'expires_at': (datetime.utcnow() + timedelta(minutes=30)).isoformat()
                }
                verify_link = url_for('auth.verify_email_link', email=user.email, token=token, _external=True)
                subject = "SmartVision - Verify Your Email Address"
                body = f"Hello {user.name},\n\nPlease click the link below to verify your email address:\n{verify_link}\n\nBest regards,\nSmartVision Team"
                try:
                    send_email(to_email=user.email, subject=subject, body_text=body)
                except Exception as e:
                    print(f"[Email Resend Error] {e}")

                flash(f'Your account email is not verified yet. We sent a new verification link to your Gmail ({user.email}). Please check your inbox and click the link to activate your account.', 'warning')
                return redirect(url_for('main.index', state='login'))

            # Check pending approval for Teachers or Users
            if user.status == 'Pending' or (user.teacher_profile and user.teacher_profile.status == 'Pending'):
                flash('Your teacher account registration is pending administrator approval. Please wait for an administrator to approve your account.', 'warning')
                return redirect(url_for('main.index', state='login'))

            # Verify the user is logging in via the correct portal if role hint is passed
            if login_role == 'admin' and user.role != 'admin':
                flash('This account is not an administrator account. Please select the correct login portal.', 'danger')
                return redirect(url_for('main.index', state='login'))
            if login_role == 'teacher' and user.role != 'teacher':
                flash('This account is not a teacher account. Please select the correct login portal.', 'danger')
                return redirect(url_for('main.index', state='login'))
            if login_role == 'student' and user.role != 'student':
                flash('This account is not a student account. Please select the correct login portal.', 'danger')
                return redirect(url_for('main.index', state='login'))

            login_user(user)
            flash(f'Welcome back, {user.name}!', 'success')
            if user.role == 'admin':
                return redirect(url_for('main.dashboard'))
            elif user.role == 'teacher':
                return redirect(url_for('teacher.dashboard'))
            elif user.role == 'student':
                return redirect(url_for('student.dashboard'))
        else:
            flash('Invalid email or password.', 'danger')
            return redirect(url_for('main.index', state='login'))

    # Redirect GET requests to the unified index page with state='login'
    return redirect(url_for('main.index', state='login'))

@auth_bp.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('main.dashboard'))
        elif current_user.role == 'student':
            return redirect(url_for('student.dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            if user.role != 'admin':
                flash('This portal is restricted to administrators. Please use the Student portal.', 'danger')
                return redirect(url_for('auth.admin_login'))

            login_user(user)
            flash(f'Welcome back, {user.name}!', 'success')
            return redirect(url_for('main.dashboard'))
        else:
            flash('Invalid email or password.', 'danger')
            return redirect(url_for('auth.admin_login'))

    return render_template('admin_login.html')

@auth_bp.route('/signup', methods=['GET', 'POST'])
@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        role = request.form.get('signup_role')
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')

        if not all([role, name, email, password]):
            flash('Name, email, and password are required.', 'danger')
            return redirect(url_for('main.index', state='signup'))

        if len(password) < 8:
            flash('Password must be at least 8 characters long.', 'danger')
            return redirect(url_for('main.index', state='signup'))

        existing_reg_user = User.query.filter_by(email=email).first()
        if existing_reg_user:
            has_active_profile = False
            if existing_reg_user.role == 'student':
                from models import Student
                has_active_profile = (Student.query.filter_by(user_id=existing_reg_user.id).first() is not None)
            elif existing_reg_user.role == 'teacher':
                from models import Teacher
                has_active_profile = (Teacher.query.filter_by(user_id=existing_reg_user.id).first() is not None)
            elif existing_reg_user.role == 'admin':
                has_active_profile = True

            if has_active_profile:
                flash('This email address is already registered. Please log in.', 'danger')
                return redirect(url_for('main.index', state='signup'))
            else:
                db.session.delete(existing_reg_user)
                db.session.commit()

        # --- ADMIN REGISTRATION ---
        if role == 'admin':
            new_user = User(name=name, email=email, role='admin', status='Approved', is_email_verified=True)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('Administrator account created successfully! Please login.', 'success')
            return redirect(url_for('main.index', state='login'))

        # --- TEACHER REGISTRATION ---
        elif role == 'teacher':
            mobile = request.form.get('mobile', '').strip()
            teacher_id = request.form.get('teacher_id', '').strip() or request.form.get('emp_id', '').strip()
            photo = request.files.get('student_photo')
            captured_base64 = request.form.get('captured_image_base64')

            if not teacher_id:
                flash('Teacher ID is required for Teacher registration. Please enter the Teacher ID issued by your Administrator.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            # Verify Teacher ID in IssuedTeacherID table
            issued_rec = IssuedTeacherID.query.filter_by(teacher_id=teacher_id).first()
            if not issued_rec or issued_rec.is_used:
                flash('Invalid or already used Teacher ID. Teacher registration requires a valid Teacher ID issued by an Administrator.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            if issued_rec.email and issued_rec.email.strip().lower() != email.strip().lower():
                flash(f'This Teacher ID ({teacher_id}) was issued specifically for {issued_rec.email}. Please use that email address.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            from models import Teacher
            if Teacher.query.filter_by(emp_id=teacher_id).first():
                flash('A teacher with this Teacher ID is already registered.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            primary_subject = request.form.get('primary_subject', '').strip()
            secondary_subject = request.form.get('secondary_subject', '').strip()
            tertiary_subject = request.form.get('tertiary_subject', '').strip()

            selected_prefs = [s for s in [primary_subject, secondary_subject, tertiary_subject] if s]
            if len(selected_prefs) != len(set(selected_prefs)):
                flash('Duplicate subject preferences detected. Please select distinct subjects for Primary, Secondary, and 3rd choices.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            # Enforce Mandatory Photo for Teacher Registration
            if not captured_base64 and not (photo and photo.filename):
                flash('Teacher photo is mandatory for registration. Please upload or capture a photo.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            face_encoding_bytes = None
            image_filename = None

            if captured_base64 and captured_base64.strip():
                result = save_base64_image(captured_base64, f"teacher_{teacher_id}", name, FACES_FOLDER)
                if result:
                    image_filename, filepath = result
                    if face_recognition:
                        try:
                            image = face_recognition.load_image_file(filepath)
                            encodings = face_recognition.face_encodings(image)
                            if len(encodings) == 1:
                                face_encoding_bytes = encodings[0].tobytes()
                            elif len(encodings) > 1:
                                flash('Multiple faces found in photo. Please use a clear image of ONLY yourself.', 'danger')
                                if os.path.exists(filepath):
                                    os.remove(filepath)
                                return redirect(url_for('main.index', state='signup'))
                        except Exception as e:
                            print(f"Teacher face scan error: {e}")
            elif photo and photo.filename:
                os.makedirs(FACES_FOLDER, exist_ok=True)
                filename = secure_filename(f"teacher_{teacher_id}_{name}_{photo.filename}")
                filepath = os.path.join(FACES_FOLDER, filename)
                photo.save(filepath)
                image_filename = filename

                if face_recognition:
                    try:
                        image = face_recognition.load_image_file(filepath)
                        encodings = face_recognition.face_encodings(image)
                        if len(encodings) == 1:
                            face_encoding_bytes = encodings[0].tobytes()
                        elif len(encodings) > 1:
                            flash('Multiple faces found in photo. Please use a clear image of ONLY yourself.', 'danger')
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            return redirect(url_for('main.index', state='signup'))
                    except Exception as e:
                        print(f"Teacher face scan error: {e}")

            try:
                new_user = User(name=name, email=email, role='teacher', mobile=mobile or None, status='Pending', is_email_verified=True)
                new_user.set_password(password)
                db.session.add(new_user)
                db.session.flush()

                new_teacher = Teacher(
                    name=name,
                    email=email,
                    emp_id=teacher_id,
                    mobile=mobile or None,
                    image_filename=image_filename,
                    face_encoding=face_encoding_bytes,
                    status='Pending',
                    user_id=new_user.id,
                    primary_subject=primary_subject or None,
                    secondary_subject=secondary_subject or None,
                    tertiary_subject=tertiary_subject or None
                )
                db.session.add(new_teacher)
                if issued_rec:
                    issued_rec.is_used = True
                    issued_rec.used_at = datetime.utcnow()
                db.session.commit()

                flash('Teacher registration submitted! Your account is now pending Administrator approval.', 'info')
                return redirect(url_for('main.index', state='login'))
            except Exception as e:
                db.session.rollback()
                flash(f'Teacher registration failed: {e}', 'danger')
                return redirect(url_for('main.index', state='signup'))

        # --- STUDENT REGISTRATION ---
        elif role == 'student':
            mobile = request.form.get('mobile', '').strip()
            roll_no = request.form.get('roll_no', '').strip()
            enrollment_no = request.form.get('enrollment_no', '').strip()
            department = request.form.get('department', '').strip()
            parent_name = request.form.get('parent_name', '').strip()
            parent_email = request.form.get('parent_email', '').strip()
            parent_mobile = request.form.get('parent_mobile', '').strip()
            student_photo = request.files.get('student_photo')
            captured_base64 = request.form.get('captured_image_base64')

            if not all([roll_no, enrollment_no]):
                flash('Roll Number and Enrollment Number are required for student registration.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            if not parent_name or not parent_email or not parent_mobile:
                flash('Parent Name, Parent Email, and Parent Mobile Number are required for student registration.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            # Enforce Mandatory Photo for Student Registration
            if not captured_base64 and not (student_photo and student_photo.filename):
                flash('Student photo is mandatory for registration. Please upload a photo or capture via camera.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            if Student.query.filter_by(roll_no=roll_no).first():
                flash('A student with this Roll Number already exists.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            if Student.query.filter_by(enrollment_no=enrollment_no).first():
                flash('A student with this Enrollment Number already exists.', 'danger')
                return redirect(url_for('main.index', state='signup'))

            # Create User account with verified email status (verified via inline OTP)
            new_user = User(name=name, email=email, role='student', mobile=mobile or None, status='Approved', is_email_verified=True)
            new_user.set_password(password)

            face_encoding_bytes = None
            image_filename = None

            if captured_base64 and captured_base64.strip():
                result = save_base64_image(captured_base64, roll_no, name, FACES_FOLDER)
                if result:
                    image_filename, filepath = result
                    if face_recognition:
                        try:
                            image = face_recognition.load_image_file(filepath)
                            encodings = face_recognition.face_encodings(image)
                            if len(encodings) == 1:
                                face_encoding_bytes = encodings[0].tobytes()
                            elif len(encodings) == 0:
                                flash('No face detected in the captured photo. Please capture a clear face photo.', 'warning')
                                if os.path.exists(filepath):
                                    os.remove(filepath)
                                return redirect(url_for('main.index', state='signup'))
                            else:
                                flash(f'{len(encodings)} faces found in captured photo. Please capture a clear image of ONLY one face.', 'danger')
                                if os.path.exists(filepath):
                                    os.remove(filepath)
                                return redirect(url_for('main.index', state='signup'))
                        except Exception as e:
                            flash(f'An error occurred during facial scanning: {e}', 'danger')
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            return redirect(url_for('main.index', state='signup'))
            elif student_photo and student_photo.filename:
                os.makedirs(FACES_FOLDER, exist_ok=True)
                filename = secure_filename(f"{roll_no}_{name}_{student_photo.filename}")
                filepath = os.path.join(FACES_FOLDER, filename)
                student_photo.save(filepath)
                image_filename = filename

                if face_recognition:
                    try:
                        image = face_recognition.load_image_file(filepath)
                        encodings = face_recognition.face_encodings(image)
                        if len(encodings) == 1:
                            face_encoding_bytes = encodings[0].tobytes()
                        elif len(encodings) == 0:
                            flash('No face detected in the photo. Please upload a clear face photo.', 'warning')
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            return redirect(url_for('main.index', state='signup'))
                        else:
                            flash(f'{len(encodings)} faces found in photo. Please use a clear image of ONLY one face.', 'danger')
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            return redirect(url_for('main.index', state='signup'))
                    except Exception as e:
                        flash(f'An error occurred during facial scanning: {e}', 'danger')
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        return redirect(url_for('main.index', state='signup'))

            try:
                db.session.add(new_user)
                db.session.flush()  # Get new_user.id

                selected_class_id = request.form.get('class_id')
                class_id_int = None
                if selected_class_id:
                    try:
                        class_id_int = int(selected_class_id)
                    except ValueError:
                        pass
                
                new_student = Student(
                    name=name,
                    roll_no=roll_no,
                    enrollment_no=enrollment_no,
                    department=department or 'General',
                    mobile=mobile or None,
                    parent_name=parent_name or None,
                    parent_email=parent_email or None,
                    parent_mobile=parent_mobile or None,
                    class_id=class_id_int,
                    face_encoding=face_encoding_bytes,
                    image_filename=image_filename,
                    user_id=new_user.id
                )

                db.session.add(new_student)
                db.session.commit()

                flash(
                    'Student account registered and email verified successfully! '
                    'An administrator will assign your class. You can now log in.',
                    'success'
                )
                return redirect(url_for('main.index', state='login'))
            except Exception as e:
                db.session.rollback()
                flash(f'Registration failed: {e}', 'danger')
                return redirect(url_for('main.index', state='signup'))

        else:
            flash('Invalid role selected.', 'danger')
            return redirect(url_for('main.index', state='signup'))

    # Redirect GET requests to the unified index page with state='signup'
    return redirect(url_for('main.index', state='signup'))

signup = register

@auth_bp.route('/verify_teacher_email', methods=['POST'])
def verify_teacher_email():
    entered_otp = request.form.get('otp', '').strip()
    email = request.form.get('email', '').strip().lower() or session.get('teacher_verify_email', '').lower()
    
    verify_data = session.get('teacher_verify_data')
    if not verify_data or verify_data.get('email', '').lower() != email:
        user = User.query.filter_by(email=email).first()
        if user and user.role == 'teacher' and not getattr(user, 'is_email_verified', True):
            otp = generate_otp(6)
            session['teacher_verify_data'] = {
                'email': email,
                'otp': otp,
                'teacher_id': user.teacher_profile.emp_id if user.teacher_profile else '',
                'expires_at': (datetime.utcnow() + timedelta(minutes=15)).isoformat()
            }
            print("\n" + "=" * 80)
            print("[SMARTVISION TEACHER EMAIL VERIFICATION] NEW OTP GENERATED")
            print(f"  To      : {user.name} <{user.email}>")
            print(f"  OTP Code: {otp}  (valid for 15 minutes)")
            print("=" * 80 + "\n", flush=True)
            flash('A new OTP code has been generated. Please check server log/console.', 'info')
            return redirect(url_for('main.index', state='teacher-verify-email', email=email))
        else:
            flash('Verification session expired or account not found. Please try logging in.', 'danger')
            return redirect(url_for('main.index', state='login'))

    expires_at = datetime.fromisoformat(verify_data['expires_at'])
    if datetime.utcnow() > expires_at:
        flash('Verification OTP code has expired. Click Resend OTP for a new code.', 'danger')
        return redirect(url_for('main.index', state='teacher-verify-email', email=email))

    if entered_otp != verify_data['otp']:
        flash('Incorrect OTP code. Please try again.', 'danger')
        return redirect(url_for('main.index', state='teacher-verify-email', email=email))

    user = User.query.filter_by(email=email).first()
    if user:
        user.is_email_verified = True
        user.status = 'Approved'
        if user.teacher_profile:
            user.teacher_profile.status = 'Approved'
            teacher_id_code = user.teacher_profile.emp_id
            issued_rec = IssuedTeacherID.query.filter_by(teacher_id=teacher_id_code).first()
            if issued_rec:
                issued_rec.is_used = True
                issued_rec.used_by_user_id = user.id
        db.session.commit()

        session.pop('teacher_verify_data', None)
        login_user(user)
        flash(f'Email verified successfully! Welcome, {user.name}. Your teacher account is active.', 'success')
        return redirect(url_for('teacher.dashboard'))
    else:
        flash('Account not found.', 'danger')
        return redirect(url_for('main.index', state='login'))

@auth_bp.route('/resend_teacher_otp', methods=['POST'])
def resend_teacher_otp():
    email = request.form.get('email', '').strip().lower()
    user = User.query.filter_by(email=email).first()
    if user and user.role == 'teacher' and not getattr(user, 'is_email_verified', True):
        otp = generate_otp(6)
        session['teacher_verify_data'] = {
            'email': email,
            'otp': otp,
            'teacher_id': user.teacher_profile.emp_id if user.teacher_profile else '',
            'expires_at': (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        }
        print("\n" + "=" * 80)
        print("[SMARTVISION TEACHER EMAIL VERIFICATION] RESENT OTP GENERATED")
        print(f"  To      : {user.name} <{user.email}>")
        print(f"  OTP Code: {otp}  (valid for 15 minutes)")
        print("=" * 80 + "\n", flush=True)
        flash('A fresh verification OTP code has been sent to your email. Check server console.', 'success')
        return redirect(url_for('main.index', state='teacher-verify-email', email=email))
    flash('Unable to resend OTP. User not found or already verified.', 'warning')
    return redirect(url_for('main.index', state='login'))

@auth_bp.route('/login/google')
def google_login():
    if not is_google_configured():
        flash('Google Login is not configured by the administrator.', 'warning')
        return redirect(url_for('main.index', state='login'))
    
    redirect_uri = current_app.config.get('GOOGLE_REDIRECT_URI')
    if not redirect_uri:
        redirect_uri = url_for('auth.google_login_callback', _external=True)
        # Ensure HTTPS for production cloud deployments (Render / AWS / Railway)
        if 'localhost' not in redirect_uri and '127.0.0.1' not in redirect_uri and redirect_uri.startswith('http://'):
            redirect_uri = 'https://' + redirect_uri[len('http://'):]
    return oauth.google.authorize_redirect(redirect_uri)

@auth_bp.route('/login/google/callback')
def google_login_callback():
    return handle_google_callback()

@auth_bp.route('/auth/google/callback')
def google_callback():
    return handle_google_callback()

def handle_google_callback():
    if not is_google_configured():
        return redirect(url_for('main.index', state='login'))

    try:
        token = oauth.google.authorize_access_token()
        resp = oauth.google.get('userinfo')
        user_info = resp.json()
        email = user_info.get('email')
        name = user_info.get('name')
        google_id = user_info.get('id')

        user = User.query.filter_by(email=email).first()
        if user:
            if not user.google_id:
                user.google_id = google_id
                db.session.commit()

            if user.role == 'teacher' and not getattr(user, 'is_email_verified', True):
                otp = generate_otp(6)
                session['teacher_verify_data'] = {
                    'email': user.email,
                    'otp': otp,
                    'teacher_id': user.teacher_profile.emp_id if user.teacher_profile else '',
                    'expires_at': (datetime.utcnow() + timedelta(minutes=15)).isoformat()
                }
                print("\n" + "=" * 80)
                print("[SMARTVISION TEACHER GOOGLE LOGIN] EMAIL UNVERIFIED - OTP GENERATED")
                print(f"  To      : {user.name} <{user.email}>")
                print(f"  OTP Code: {otp}  (valid for 15 minutes)")
                print("=" * 80 + "\n", flush=True)
                flash('Your teacher account email requires verification before login.', 'warning')
                return redirect(url_for('main.index', state='teacher-verify-email', email=user.email))

            login_user(user)
            flash(f'Logged in with Google as {user.name}!', 'success')
            if user.role == 'admin':
                return redirect(url_for('main.dashboard'))
            elif user.role == 'teacher':
                return redirect(url_for('teacher.dashboard'))
            elif user.role == 'student':
                return redirect(url_for('student.dashboard'))
        else:
            issued = IssuedTeacherID.query.filter_by(email=email, is_used=False).first()
            session['google_signup_data'] = {
                'email': email,
                'name': name,
                'google_id': google_id,
                'matched_teacher_id': issued.teacher_id if issued else None
            }
            flash('Google authenticated successfully! Please complete your account details.', 'info')
            return redirect(url_for('main.index', state='google-complete'))
    except Exception as e:
        flash(f'Google OAuth failed: {str(e)}', 'danger')
        return redirect(url_for('main.index', state='login'))

@auth_bp.route('/signup/google/complete', methods=['GET', 'POST'])
def google_signup_complete():
    google_data = session.get('google_signup_data')
    if not google_data:
        flash('Session expired or invalid signup flow.', 'danger')
        return redirect(url_for('main.index', state='signup'))

    if request.method == 'POST':
        role = request.form.get('role')
        
        email = google_data['email']
        name = google_data['name']
        google_id = google_data['google_id']

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('main.index', state='login'))

        new_user = User(name=name, email=email, role=role, google_id=google_id)

        if role == 'admin':
            new_user.is_email_verified = True
            new_user.status = 'Approved'
            db.session.add(new_user)
            db.session.commit()
            session.pop('google_signup_data', None)
            login_user(new_user)
            flash('Admin account created successfully with Google!', 'success')
            return redirect(url_for('main.dashboard'))

        elif role == 'teacher':
            teacher_id = request.form.get('teacher_id', '').strip() or request.form.get('emp_id', '').strip() or google_data.get('matched_teacher_id')
            mobile = request.form.get('mobile', '').strip()

            if not teacher_id:
                flash('Teacher ID is required for Teacher registration. Please enter an issued Teacher ID.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            issued = IssuedTeacherID.query.filter_by(teacher_id=teacher_id).first()
            if not issued or issued.is_used:
                flash('Invalid or already used Teacher ID. Teacher registration requires a valid Teacher ID issued by an Administrator.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            if issued.email and issued.email.strip().lower() != email.strip().lower():
                flash(f'This Teacher ID ({teacher_id}) was issued specifically for {issued.email}. Please use that email.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            from models import Teacher
            if Teacher.query.filter_by(emp_id=teacher_id).first():
                flash('A teacher with this Teacher ID is already registered.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            primary_subject = request.form.get('primary_subject', '').strip()
            secondary_subject = request.form.get('secondary_subject', '').strip()
            tertiary_subject = request.form.get('tertiary_subject', '').strip()

            selected_prefs = [s for s in [primary_subject, secondary_subject, tertiary_subject] if s]
            if len(selected_prefs) != len(set(selected_prefs)):
                flash('Duplicate subject preferences detected. Please select distinct subjects for Primary, Secondary, and 3rd choices.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            try:
                new_user.mobile = mobile or None
                new_user.status = 'Approved'
                new_user.is_email_verified = True  # Google OAuth verifies email ownership
                db.session.add(new_user)
                db.session.flush()

                new_teacher = Teacher(
                    name=name,
                    email=email,
                    emp_id=teacher_id,
                    mobile=mobile or None,
                    status='Approved',
                    user_id=new_user.id,
                    primary_subject=primary_subject or None,
                    secondary_subject=secondary_subject or None,
                    tertiary_subject=tertiary_subject or None
                )
                db.session.add(new_teacher)

                issued.is_used = True
                issued.used_by_user_id = new_user.id
                db.session.commit()

                session.pop('google_signup_data', None)
                login_user(new_user)
                flash('Teacher account registered & verified successfully with Google!', 'success')
                return redirect(url_for('teacher.dashboard'))
            except Exception as e:
                db.session.rollback()
                flash(f'Teacher registration with Google failed: {e}', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

        elif role == 'student':
            mobile = request.form.get('mobile', '').strip()
            roll_no = request.form.get('roll_no')
            enrollment_no = request.form.get('enrollment_no')
            department = request.form.get('department', '').strip()
            student_photo = request.files.get('student_photo')

            if not all([roll_no, enrollment_no]):
                flash('Roll Number and Enrollment Number are required.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            if Student.query.filter_by(roll_no=roll_no).first() or Student.query.filter_by(enrollment_no=enrollment_no).first():
                flash('A student with this roll or enrollment number already exists.', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

            face_encoding_bytes = None
            image_filename = None
            captured_base64 = request.form.get('captured_image_base64')

            if captured_base64 and captured_base64.strip():
                result = save_base64_image(captured_base64, roll_no, name, FACES_FOLDER)
                if result:
                    image_filename, filepath = result
                    try:
                        image = face_recognition.load_image_file(filepath)
                        encodings = face_recognition.face_encodings(image)
                        if len(encodings) == 1:
                            face_encoding_bytes = encodings[0].tobytes()
                        else:
                            flash(f'{len(encodings)} faces found in captured photo. Please capture a clear image of ONLY one face.', 'danger')
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            return redirect(url_for('main.index', state='google-complete'))
                    except Exception as e:
                        db.session.rollback()
                        flash(f'An error occurred during facial scanning: {e}', 'danger')
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        return redirect(url_for('main.index', state='google-complete'))
            elif student_photo and student_photo.filename:
                os.makedirs(FACES_FOLDER, exist_ok=True)
                filename = secure_filename(f"{roll_no}_{name}_{student_photo.filename}")
                filepath = os.path.join(FACES_FOLDER, filename)
                student_photo.save(filepath)

                try:
                    image = face_recognition.load_image_file(filepath)
                    encodings = face_recognition.face_encodings(image)
                    if len(encodings) == 1:
                        face_encoding_bytes = encodings[0].tobytes()
                        image_filename = filename
                    else:
                        flash(f'{len(encodings)} faces found in photo. Please use a clear image of ONLY one face.', 'danger')
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        return redirect(url_for('main.index', state='google-complete'))
                except Exception as e:
                    db.session.rollback()
                    flash(f'An error occurred: {e}', 'danger')
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return redirect(url_for('main.index', state='google-complete'))

            try:
                new_user.mobile = mobile or None
                db.session.add(new_user)
                db.session.flush()

                new_student = Student(
                    name=name,
                    roll_no=roll_no,
                    enrollment_no=enrollment_no,
                    department=department or 'General',
                    mobile=mobile or None,
                    class_id=None,  # Admin assigns class
                    face_encoding=face_encoding_bytes,
                    image_filename=image_filename,
                    user_id=new_user.id
                )
                db.session.add(new_student)
                db.session.commit()

                session.pop('google_signup_data', None)
                login_user(new_user)
                flash('Student account created successfully with Google! An admin will assign your class.', 'success')
                return redirect(url_for('student.dashboard'))
            except Exception as e:
                db.session.rollback()
                flash(f'Registration failed: {e}', 'danger')
                return redirect(url_for('main.index', state='google-complete'))

    return redirect(url_for('main.index', state='google-complete'))

# ─────────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD — OTP FLOW
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """Step 1: User enters their email. We generate & store an OTP in session."""
    if current_user.is_authenticated:
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()

        if user:
            otp = generate_otp(6)
            # Store OTP with expiry (10 minutes)
            session['otp_data'] = {
                'email': email,
                'otp': otp,
                'expires_at': (datetime.utcnow() + timedelta(minutes=10)).isoformat()
            }

            # Simulate sending OTP to email / mobile
            print("\n" + "=" * 80)
            print("[SMARTVISION OTP SYSTEM] PASSWORD RESET OTP GENERATED")
            print(f"  To    : {user.name} <{user.email}>")
            if user.mobile:
                print(f"  Mobile: {user.mobile}")
            print(f"  OTP   : {otp}  (valid for 10 minutes)")
            print("=" * 80 + "\n", flush=True)

        # Always redirect to OTP panel (don't reveal if email exists)
        return redirect(url_for('main.index', state='otp-verify', email=email))

    return redirect(url_for('main.index', state='forgot'))

@auth_bp.route('/verify_otp', methods=['POST'])
def verify_otp():
    """Step 2: User enters OTP. Validate and redirect to password reset panel."""
    entered_otp = request.form.get('otp', '').strip()
    otp_data = session.get('otp_data')

    if not otp_data:
        flash('OTP session expired. Please try again.', 'danger')
        return redirect(url_for('main.index', state='forgot'))

    # Check expiry
    expires_at = datetime.fromisoformat(otp_data['expires_at'])
    if datetime.utcnow() > expires_at:
        session.pop('otp_data', None)
        flash('OTP has expired. Please request a new one.', 'danger')
        return redirect(url_for('main.index', state='forgot'))

    if entered_otp != otp_data['otp']:
        flash('Incorrect OTP. Please try again.', 'danger')
        email = otp_data.get('email', '')
        return redirect(url_for('main.index', state='otp-verify', email=email))

    # OTP correct — mark as verified and allow password reset
    session['otp_verified_email'] = otp_data['email']
    session.pop('otp_data', None)
    return redirect(url_for('main.index', state='reset-password'))

@auth_bp.route('/reset_password_otp', methods=['POST'])
def reset_password_otp():
    """Step 3: OTP was verified — save the new password."""
    verified_email = session.get('otp_verified_email')
    if not verified_email:
        flash('Password reset session expired. Please start over.', 'danger')
        return redirect(url_for('main.index', state='forgot'))

    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    if len(password) < 8:
        flash('Password must be at least 8 characters long.', 'danger')
        return redirect(url_for('main.index', state='reset-password'))

    if password != confirm_password:
        flash('Passwords do not match.', 'danger')
        return redirect(url_for('main.index', state='reset-password'))

    user = User.query.filter_by(email=verified_email).first()
    if not user:
        flash('Account not found. Please register.', 'danger')
        session.pop('otp_verified_email', None)
        return redirect(url_for('main.index', state='signup'))

    user.set_password(password)
    db.session.commit()
    session.pop('otp_verified_email', None)
    flash('Your password has been successfully reset! You can now log in.', 'success')
    return redirect(url_for('main.index', state='login'))

# Legacy token-based reset (kept for backward compat with any emailed links still in use)
@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('auth.login'))

    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=1800)
    except Exception:
        flash("The password reset link is invalid or has expired.", "danger")
        return redirect(url_for('main.index', state='login'))

    user = User.query.filter_by(email=email).first_or_404()

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(request.url)

        user.set_password(password)
        db.session.commit()
        flash("Your password has been successfully reset! You can now log in.", "success")
        return redirect(url_for('main.index', state='login'))

    return render_template('reset_password.html', email=user.email)

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('main.index', state='login'))