# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - DATABASE ORM MODELS (SQLAlchemy)
# ==============================================================================
# Description: Central database schema definitions including User Accounts, Roles,
#              Academic Departments, Classes, Subjects, Timetables, Daily Schedules,
#              Student & Teacher Profiles, Face Embeddings, Attendance Engine Sessions,
#              Geofenced Faculty Check-ins, Leaves, Discrepancies, and Audit Logs.
# ==============================================================================

from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db, login_manager

# ==============================================================================
# SECTION 1: USER AUTHENTICATION & CORE PROFILE MODELS
# ==============================================================================

class User(UserMixin, db.Model):
    """
    Core User entity for authentication, role-based access control,
    Google OAuth single sign-on, and credential management.
    """
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), default='user') # 'admin', 'teacher', or 'student'
    status = db.Column(db.String(20), default='Approved') # 'Pending', 'Approved', 'Rejected', 'Email_Unverified'
    google_id = db.Column(db.String(100), nullable=True)
    is_email_verified = db.Column(db.Boolean, default=True)

    @property
    def mobile(self):
        return self.phone

    @mobile.setter
    def mobile(self, value):
        self.phone = value

    # One-to-One Relationships with Student and Teacher Profiles
    student_profile = db.relationship('Student', backref='user_account', uselist=False, lazy=True, foreign_keys='Student.user_id')
    teacher_profile = db.relationship('Teacher', backref='user_account', uselist=False, lazy=True, foreign_keys='Teacher.user_id')

    def set_password(self, password):
        """Hashes password using secure sha256 algorithm."""
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        """Verifies provided raw password against stored hash."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)


class IssuedTeacherID(db.Model):
    """
    Pre-issued authorization tokens/IDs created by Admin for inviting new faculty.
    """
    __tablename__ = 'issued_teacher_ids'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), nullable=True)
    name = db.Column(db.String(100), nullable=True)
    is_used = db.Column(db.Boolean, default=False)
    used_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


# ==============================================================================
# SECTION 2: ACADEMIC INFRASTRUCTURE MODELS (DEPARTMENTS, CLASSES, SUBJECTS)
# ==============================================================================

class Department(db.Model):
    """Academic Department (e.g. CSE, AI/ML, AI/DS, IT, ECE)."""
    __tablename__ = 'departments'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    full_name = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Class(db.Model):
    """Class Section (e.g. Class 7CSE4) assigned to a Class Teacher and Department."""
    __tablename__ = 'classes'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    section = db.Column(db.String(50), nullable=True)
    department = db.Column(db.String(100), nullable=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    class_teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id'), nullable=True)

    # Relationships
    students = db.relationship('Student', backref='class_assigned', lazy=True, cascade="all, delete-orphan")
    subjects = db.relationship('Subject', backref='class_assigned', lazy=True, cascade="all, delete-orphan")
    class_teacher = db.relationship('Teacher', backref='classes_directed', foreign_keys=[class_teacher_id])
    teacher_assignments = db.relationship('TeacherAssignment', backref='class_assigned', lazy=True, cascade="all, delete-orphan")


class Subject(db.Model):
    """Course / Subject taught within a specific Class Section by an assigned Teacher."""
    __tablename__ = 'subjects'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id'), nullable=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    teacher_assignments = db.relationship('TeacherAssignment', backref='subject_assigned', lazy=True, cascade="all, delete-orphan")


class TeacherAssignment(db.Model):
    """Cross-mapping between Teachers, Classes, and Subjects for multi-course allocation."""
    __tablename__ = 'teacher_assignments'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id', ondelete='CASCADE'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='CASCADE'), nullable=False)


# ==============================================================================
# SECTION 3: STUDENT & TEACHER ENTITY PROFILES (FACE BIOMETRICS & EMBEDDINGS)
# ==============================================================================

class Teacher(db.Model):
    """
    Faculty profile entity holding employee codes, face embeddings,
    teaching preferences, assigned subjects, and leave records.
    """
    __tablename__ = 'teachers'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, unique=True)
    name = db.Column(db.String(100), nullable=False)
    emp_id = db.Column(db.String(50), nullable=True)
    employee_code = db.Column(db.String(50), nullable=True)
    department = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(100), nullable=True)
    mobile = db.Column(db.String(20), nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    image_data = db.Column(db.Text, nullable=True) # Permanent base64 persistence in Neon DB
    face_encoding = db.Column(db.LargeBinary, nullable=True)
    status = db.Column(db.String(20), default='Approved')
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    
    # Teaching Preferences & Subject Expertise
    primary_subject = db.Column(db.String(100), nullable=True)
    secondary_subject = db.Column(db.String(100), nullable=True)
    tertiary_subject = db.Column(db.String(100), nullable=True)

    def __init__(self, **kwargs):
        if 'emp_id' in kwargs and 'employee_code' not in kwargs:
            kwargs['employee_code'] = kwargs['emp_id']
        elif 'employee_code' in kwargs and 'emp_id' not in kwargs:
            kwargs['emp_id'] = kwargs['employee_code']
        super().__init__(**kwargs)

    subjects = db.relationship('Subject', backref='teacher', lazy=True)
    assignments = db.relationship('TeacherAssignment', backref='teacher', lazy=True, cascade="all, delete-orphan")
    leaves = db.relationship('TeacherLeave', foreign_keys='TeacherLeave.teacher_id', backref='teacher', lazy=True)

    @property
    def effective_department(self):
        """Returns the assigned department or intelligently resolves from directed classes or subjects."""
        if self.department and self.department.strip().lower() not in ['general', 'none', '', 'null']:
            return self.department.strip()

        # 1. Check classes directed as class teacher
        if self.classes_directed:
            for c in self.classes_directed:
                if c.department and c.department.strip().lower() not in ['general', 'none', '', 'null']:
                    return c.department.strip()
                cname = (c.name or '').upper()
                for d in ['CSE', 'AIML', 'AIDS', 'IT', 'ECE', 'MECH', 'CIVIL', 'ELECTRICAL']:
                    if d in cname:
                        return d

        # 2. Check assigned subjects
        if self.subjects:
            for sub in self.subjects:
                if sub.class_assigned:
                    if sub.class_assigned.department and sub.class_assigned.department.strip().lower() not in ['general', 'none', '', 'null']:
                        return sub.class_assigned.department.strip()
                    cname = (sub.class_assigned.name or '').upper()
                    for d in ['CSE', 'AIML', 'AIDS', 'IT', 'ECE', 'MECH', 'CIVIL', 'ELECTRICAL']:
                        if d in cname:
                            return d

        # 3. Check assignments
        if self.assignments:
            for a in self.assignments:
                if a.class_assigned:
                    if a.class_assigned.department and a.class_assigned.department.strip().lower() not in ['general', 'none', '', 'null']:
                        return a.class_assigned.department.strip()
                    cname = (a.class_assigned.name or '').upper()
                    for d in ['CSE', 'AIML', 'AIDS', 'IT', 'ECE', 'MECH', 'CIVIL', 'ELECTRICAL']:
                        if d in cname:
                            return d

        return self.department or 'General'


class Student(db.Model):
    """
    Enrolled Student profile containing Roll Number, Enrollment Number,
    128-dimensional Facial Biometric Embeddings, and parent contact details.
    """
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, unique=True)
    name = db.Column(db.String(100), nullable=False)
    roll_no = db.Column(db.String(50), nullable=True)
    roll_number = db.Column(db.String(50), nullable=True)
    enrollment_no = db.Column(db.String(50), nullable=False)
    department = db.Column(db.String(100), nullable=True)
    mobile = db.Column(db.String(20), nullable=True)
    parent_name = db.Column(db.String(100), nullable=True)
    parent_email = db.Column(db.String(100), nullable=True)
    parent_mobile = db.Column(db.String(20), nullable=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=True)
    face_encoding = db.Column(db.LargeBinary, nullable=True)
    face_embedding = db.Column(db.LargeBinary, nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    image_data = db.Column(db.Text, nullable=True) # Permanent base64 persistence in Neon DB

    def __init__(self, **kwargs):
        if 'roll_no' in kwargs and 'roll_number' not in kwargs:
            kwargs['roll_number'] = kwargs['roll_no']
        elif 'roll_number' in kwargs and 'roll_no' not in kwargs:
            kwargs['roll_no'] = kwargs['roll_number']
        if 'face_encoding' in kwargs and 'face_embedding' not in kwargs:
            kwargs['face_embedding'] = kwargs['face_encoding']
        elif 'face_embedding' in kwargs and 'face_encoding' not in kwargs:
            kwargs['face_encoding'] = kwargs['face_embedding']
        super().__init__(**kwargs)

    attendance_records = db.relationship('AttendanceRecord', backref='student', lazy=True, cascade="all, delete-orphan")


class StudentEditRequest(db.Model):
    """Student profile modification requests submitted for Admin approval."""
    __tablename__ = 'student_edit_requests'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    new_name = db.Column(db.String(100), nullable=False)
    new_roll_no = db.Column(db.String(50), nullable=False)
    new_enrollment_no = db.Column(db.String(50), nullable=False)
    new_department = db.Column(db.String(100), nullable=True)
    new_mobile = db.Column(db.String(20), nullable=True)
    new_class_id = db.Column(db.Integer, db.ForeignKey('classes.id', ondelete='CASCADE'), nullable=False)
    new_parent_name = db.Column(db.String(100), nullable=True)
    new_parent_email = db.Column(db.String(100), nullable=True)
    new_parent_mobile = db.Column(db.String(20), nullable=True)
    new_image_filename = db.Column(db.String(255), nullable=True)
    new_image_data = db.Column(db.Text, nullable=True) # Permanent base64 persistence in Neon DB
    new_face_encoding = db.Column(db.LargeBinary, nullable=True)
    status = db.Column(db.String(20), default='Pending') # 'Pending', 'Approved', 'Rejected'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref=db.backref('edit_requests', lazy=True, cascade='all, delete-orphan'))
    class_assigned = db.relationship('Class')


# ==============================================================================
# SECTION 4: TIMETABLE & DAILY SCHEDULE ENGINE MODELS
# ==============================================================================

class Timetable(db.Model):
    """
    Standard weekly recurring timetable slot definition across days of the week.
    """
    __tablename__ = 'timetables'
    id = db.Column(db.Integer, primary_key=True)
    day_of_week = db.Column(db.String(20), nullable=False) # 'Monday', 'Tuesday', ...
    period_no = db.Column(db.Integer, nullable=True, default=1)
    start_time = db.Column(db.String(10), nullable=False)
    end_time = db.Column(db.String(10), nullable=False)
    slot_type = db.Column(db.String(20), default='CLASS') # 'CLASS', 'LIBRARY', 'OTHER', 'LUNCH', 'BREAK'
    custom_title = db.Column(db.String(100), nullable=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id', ondelete='CASCADE'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='SET NULL'), nullable=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='SET NULL'), nullable=True)
    room = db.Column(db.String(50), nullable=True)
    effective_from = db.Column(db.Date, nullable=True, default=date.today)
    effective_to = db.Column(db.Date, nullable=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    @property
    def period(self):
        return self.period_no

    @period.setter
    def period(self, value):
        self.period_no = value

    @property
    def room_number(self):
        return self.room

    @room_number.setter
    def room_number(self, value):
        self.room = value

    class_assigned = db.relationship('Class', backref=db.backref('timetable_entries', lazy=True, cascade='all, delete-orphan'))
    subject_assigned = db.relationship('Subject', backref=db.backref('timetable_entries', lazy=True))
    teacher_assigned = db.relationship('Teacher', backref=db.backref('timetable_entries', lazy=True))


class TimetablePeriodSetting(db.Model):
    """Institutional period time-windows and labels (e.g. Period 1, Lunch Break)."""
    __tablename__ = 'timetable_period_settings'
    id = db.Column(db.Integer, primary_key=True)
    period_no = db.Column(db.Integer, nullable=False, default=1)
    label = db.Column(db.String(50), nullable=False)
    start_time = db.Column(db.String(10), nullable=False)
    end_time = db.Column(db.String(10), nullable=False)
    is_lunch = db.Column(db.Boolean, default=False)
    order_index = db.Column(db.Integer, default=1)


class Holiday(db.Model):
    """Academic calendar holidays impacting class schedules."""
    __tablename__ = 'holidays'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    scope = db.Column(db.String(50), default='ALL') # 'ALL' or class_id
    reason = db.Column(db.String(255), nullable=True)


class DailySchedule(db.Model):
    """
    Date-specific daily instance of a Timetable period slot.
    Tracks proxy allocations, faculty leaves, and class cancellations.
    """
    __tablename__ = 'daily_schedule'
    __table_args__ = (
        db.UniqueConstraint('date', 'timetable_id', name='_daily_schedule_uc'),
    )
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    timetable_id = db.Column(db.Integer, db.ForeignKey('timetables.id', ondelete='CASCADE'), nullable=False, index=True)
    resolved_status = db.Column(db.String(30), default='SCHEDULED') # 'SCHEDULED', 'HOLIDAY', 'TEACHER_ON_LEAVE', 'SUBSTITUTE_ASSIGNED', 'CANCELLED'
    
    substitute_teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='SET NULL'), nullable=True)
    is_cancelled = db.Column(db.Boolean, default=False)
    cancellation_reason = db.Column(db.String(255), nullable=True)
    is_proxy = db.Column(db.Boolean, default=False)
    proxy_assigned_by_admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    proxy_assigned_at = db.Column(db.DateTime, nullable=True)

    timetable = db.relationship('Timetable', backref=db.backref('daily_schedules', lazy=True, cascade='all, delete-orphan'))
    substitute_teacher = db.relationship('Teacher', foreign_keys=[substitute_teacher_id])


# ==============================================================================
# SECTION 5: ATTENDANCE RECORDING & SESSION ENGINE MODELS
# ==============================================================================

class AttendanceSession(db.Model):
    """
    Live or completed attendance session for a class lecture slot on a given date.
    """
    __tablename__ = 'attendance_sessions'
    id = db.Column(db.Integer, primary_key=True)
    timetable_id = db.Column(db.Integer, db.ForeignKey('timetables.id', ondelete='SET NULL'), nullable=True)
    daily_schedule_id = db.Column(db.Integer, db.ForeignKey('daily_schedule.id', ondelete='SET NULL'), nullable=True)
    date = db.Column(db.Date, nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id'), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    start_time = db.Column(db.String(10), nullable=True)
    end_time = db.Column(db.String(10), nullable=True)
    status = db.Column(db.String(20), default='ATTENDANCE_OPEN') # 'SCHEDULED', 'ATTENDANCE_OPEN', 'COMPLETED', 'CANCELLED'

    teacher = db.relationship('Teacher')
    class_assigned = db.relationship('Class')
    subject = db.relationship('Subject')
    timetable = db.relationship('Timetable')
    daily_schedule = db.relationship('DailySchedule')
    records = db.relationship('AttendanceRecord', backref='session', lazy=True, cascade="all, delete-orphan")


class AttendanceRecord(db.Model):
    """
    Individual student attendance status (PRESENT / ABSENT) for a specific session.
    """
    __tablename__ = 'attendance_records'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('attendance_sessions.id', ondelete='CASCADE'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    status = db.Column(db.String(10), nullable=False) # 'PRESENT', 'ABSENT'
    confidence = db.Column(db.Float, nullable=True)
    marked_by = db.Column(db.String(20), default='CAMERA') # 'CAMERA', 'MANUAL', 'PROXY'
    marked_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('session_id', 'student_id', name='uq_session_student'),
    )


class Attendance(db.Model):
    """Legacy compatibility model supporting direct queries and daily syncs."""
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(10), nullable=False) # 'Present' or 'Absent'
    time_marked = db.Column(db.String(20), nullable=True)
    timetable_id = db.Column(db.Integer, db.ForeignKey('timetables.id'), nullable=True)

    subject = db.relationship('Subject', backref='legacy_attendance_records', lazy=True)
    timetable_entry = db.relationship('Timetable', backref='legacy_attendance_records', lazy=True)


class CorrectionRequest(db.Model):
    """Attendance correction requests submitted for administrative review."""
    __tablename__ = 'correction_requests'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('attendance_sessions.id', ondelete='CASCADE'), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='PENDING') # 'PENDING', 'APPROVED', 'REJECTED'
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    session = db.relationship('AttendanceSession')
    requester = db.relationship('User', foreign_keys=[requested_by])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])


class AttendanceUnlockPermission(db.Model):
    """Temporary admin authorization granting faculty permission to edit past attendance."""
    __tablename__ = 'attendance_unlock_permissions'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=True)
    date = db.Column(db.Date, nullable=False)
    unlocked_until = db.Column(db.DateTime, nullable=False)
    granted_by_admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    teacher = db.relationship('Teacher')
    subject = db.relationship('Subject')


class AttendanceDiscrepancyRequest(db.Model):
    """Student attendance dispute/correction requests submitted to faculty."""
    __tablename__ = 'attendance_discrepancy_requests'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey('attendance_sessions.id', ondelete='CASCADE'), nullable=True)
    class_name = db.Column(db.String(50), nullable=True)
    teacher_name = db.Column(db.String(100), nullable=True)
    lecture_time = db.Column(db.String(50), nullable=True)
    reason = db.Column(db.Text, nullable=False)
    image_proof = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='PENDING') # 'PENDING', 'APPROVED', 'REJECTED'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref=db.backref('discrepancy_requests', lazy=True, cascade='all, delete-orphan'))
    session = db.relationship('AttendanceSession', backref=db.backref('discrepancy_requests', lazy=True, cascade='all, delete-orphan'))


class AttendanceAuditLog(db.Model):
    """Immutable audit trail of all manual edits made to student attendance records."""
    __tablename__ = 'attendance_audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    attendance_id = db.Column(db.Integer, db.ForeignKey('attendance.id', ondelete='SET NULL'), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=True)
    date = db.Column(db.Date, nullable=True)
    previous_status = db.Column(db.String(20), nullable=True)
    new_status = db.Column(db.String(20), nullable=True)
    changed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    changed_by_role = db.Column(db.String(20), nullable=False)
    rationale = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    changed_by = db.relationship('User')
    student = db.relationship('Student')
    subject = db.relationship('Subject')


# ==============================================================================
# SECTION 6: FACULTY GEOFENCED ATTENDANCE & LEAVE MANAGEMENT MODELS
# ==============================================================================

class TeacherOfficeLocation(db.Model):
    """Campus Geofence boundaries (GPS Coordinates & Radius) for faculty check-in."""
    __tablename__ = 'teacher_office_locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, default="Admin Office")
    latitude = db.Column(db.Float, nullable=False, default=22.2887)
    longitude = db.Column(db.Float, nullable=False, default=73.3634)
    allowed_radius = db.Column(db.Float, nullable=False, default=50.0) # in meters
    status = db.Column(db.String(20), default='Active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TeacherAttendanceSettings(db.Model):
    """Institutional parameters for morning check-in cutoff, evening windows, and grace periods."""
    __tablename__ = 'teacher_attendance_settings'
    id = db.Column(db.Integer, primary_key=True)
    morning_start_time = db.Column(db.String(10), default="08:00 AM")
    morning_deadline = db.Column(db.String(10), default="11:00 AM")
    evening_start_time = db.Column(db.String(10), default="04:00 PM")
    evening_end_time = db.Column(db.String(10), default="07:00 PM")
    grace_period_mins = db.Column(db.Integer, default=15)
    max_gps_accuracy_meters = db.Column(db.Float, default=50.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TeacherDailyAttendance(db.Model):
    """
    Daily faculty attendance log tracking morning geofenced check-in,
    facial verification snapshot, evening checkout, and late status.
    """
    __tablename__ = 'teacher_daily_attendances'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    attendance_date = db.Column(db.Date, nullable=False, default=date.today)
    
    # Morning Check-In Biometrics & Geolocation
    check_in_at = db.Column(db.DateTime, nullable=True)
    check_in_latitude = db.Column(db.Float, nullable=True)
    check_in_longitude = db.Column(db.Float, nullable=True)
    check_in_accuracy = db.Column(db.Float, nullable=True)
    check_in_distance = db.Column(db.Float, nullable=True)
    check_in_face_verified = db.Column(db.Boolean, default=False)
    check_in_photo = db.Column(db.String(255), nullable=True)
    
    # Evening Check-Out Biometrics & Geolocation
    check_out_at = db.Column(db.DateTime, nullable=True)
    check_out_latitude = db.Column(db.Float, nullable=True)
    check_out_longitude = db.Column(db.Float, nullable=True)
    check_out_accuracy = db.Column(db.Float, nullable=True)
    check_out_distance = db.Column(db.Float, nullable=True)
    check_out_face_verified = db.Column(db.Boolean, default=False)
    check_out_photo = db.Column(db.String(255), nullable=True)
    
    office_location_id = db.Column(db.Integer, db.ForeignKey('teacher_office_locations.id', ondelete='SET NULL'), nullable=True)
    
    status = db.Column(db.String(30), default='Pending') # 'Pending', 'Present', 'Half Day', 'Absent', 'Approved Leave', 'Official Duty', 'Holiday', 'Weekend'
    late_status = db.Column(db.String(20), default='On Time') # 'On Time', 'Late', 'Uninformed Absent'
    late_minutes = db.Column(db.Integer, default=0)
    is_admin_overridden = db.Column(db.Boolean, default=False)
    informed_admin = db.Column(db.Boolean, default=False)
    absence_reason = db.Column(db.String(255), nullable=True)
    is_uninformed_absence = db.Column(db.Boolean, default=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    teacher = db.relationship('Teacher', backref=db.backref('daily_attendances', lazy=True, cascade='all, delete-orphan'))
    office_location = db.relationship('TeacherOfficeLocation')

    __table_args__ = (
        db.UniqueConstraint('teacher_id', 'attendance_date', name='uq_teacher_daily_attendance'),
    )


class TeacherLeave(db.Model):
    """Faculty leave applications with designated substitute faculty."""
    __tablename__ = 'teacher_leave'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    date_from = db.Column(db.Date, nullable=False)
    date_to = db.Column(db.Date, nullable=False)
    leave_type = db.Column(db.String(20), default='FULL') # 'FULL', 'HALF'
    status = db.Column(db.String(20), default='PENDING') # 'PENDING', 'APPROVED', 'REJECTED'
    substitute_teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id'), nullable=True)
    reason = db.Column(db.Text, nullable=True)

    substitute_teacher = db.relationship('Teacher', foreign_keys=[substitute_teacher_id])


# ==============================================================================
# SECTION 7: PROXY ATTENDANCE & CLASS TRANSFER MODELS
# ==============================================================================

class ProxyAttendanceTransfer(db.Model):
    """
    Records attendance captured by a substitute faculty member and transfers
    the present roll numbers to the original absent faculty member.
    """
    __tablename__ = 'proxy_attendance_transfers'
    id = db.Column(db.Integer, primary_key=True)
    substitute_teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    original_teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    leave_id = db.Column(db.Integer, db.ForeignKey('teacher_leave.id', ondelete='SET NULL'), nullable=True)
    timetable_id = db.Column(db.Integer, db.ForeignKey('timetables.id', ondelete='SET NULL'), nullable=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id', ondelete='CASCADE'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='CASCADE'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time_slot = db.Column(db.String(30), nullable=True)
    present_rolls = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='PENDING') # 'PENDING', 'SHARED', 'APPLIED'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    substitute_teacher = db.relationship('Teacher', foreign_keys=[substitute_teacher_id])
    original_teacher = db.relationship('Teacher', foreign_keys=[original_teacher_id])
    class_assigned = db.relationship('Class')
    subject = db.relationship('Subject')
    timetable = db.relationship('Timetable')


# ==============================================================================
# SECTION 8: ANNOUNCEMENTS, NOTICES & AUDIT LOGGING MODELS
# ==============================================================================

class ClassAnnouncement(db.Model):
    """Institutional circulars, notices, and academic announcements."""
    __tablename__ = 'class_announcements'
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id', ondelete='CASCADE'), nullable=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id'), nullable=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    posted_by_role = db.Column(db.String(20), default='teacher') # 'admin', 'teacher'
    target_role = db.Column(db.String(20), default='STUDENTS') # 'TEACHERS', 'STUDENTS', 'ALL'
    title = db.Column(db.String(150), nullable=False)
    content = db.Column(db.Text, nullable=False)
    notice_type = db.Column(db.String(30), default='Announcement')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class_assigned = db.relationship('Class', backref=db.backref('announcements', lazy=True, cascade='all, delete-orphan'))
    teacher = db.relationship('Teacher')
    admin_user = db.relationship('User', foreign_keys=[admin_id])


class StudentDismissedNotice(db.Model):
    __tablename__ = 'student_dismissed_notices'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    announcement_id = db.Column(db.Integer, db.ForeignKey('class_announcements.id', ondelete='CASCADE'), nullable=False)
    dismissed_at = db.Column(db.DateTime, default=datetime.utcnow)


class StudentReadNotice(db.Model):
    __tablename__ = 'student_read_notices'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    announcement_id = db.Column(db.Integer, db.ForeignKey('class_announcements.id', ondelete='CASCADE'), nullable=False)
    read_at = db.Column(db.DateTime, default=datetime.utcnow)


class TeacherDismissedNotice(db.Model):
    __tablename__ = 'teacher_dismissed_notices'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    announcement_id = db.Column(db.Integer, db.ForeignKey('class_announcements.id', ondelete='CASCADE'), nullable=False)
    dismissed_at = db.Column(db.DateTime, default=datetime.utcnow)


class TeacherReadNotice(db.Model):
    __tablename__ = 'teacher_read_notices'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    announcement_id = db.Column(db.Integer, db.ForeignKey('class_announcements.id', ondelete='CASCADE'), nullable=False)
    read_at = db.Column(db.DateTime, default=datetime.utcnow)


class TeacherAttendanceAuditLog(db.Model):
    """Audit log of changes and manual administrative overrides made to faculty attendance."""
    __tablename__ = 'teacher_attendance_audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    teacher_daily_attendance_id = db.Column(db.Integer, db.ForeignKey('teacher_daily_attendances.id', ondelete='CASCADE'), nullable=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teachers.id', ondelete='CASCADE'), nullable=False)
    attendance_date = db.Column(db.Date, nullable=False)
    action = db.Column(db.String(50), nullable=False)
    previous_status = db.Column(db.String(30), nullable=True)
    new_status = db.Column(db.String(30), nullable=False)
    changed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rationale = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    teacher = db.relationship('Teacher')
    changed_by = db.relationship('User')


class UniversitySettings(db.Model):
    """
    Institutional metadata, branding, leadership details, and logos
    reflected across all admin, teacher, student portals, landing page, and ID cards.
    """
    __tablename__ = 'university_settings'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), default='SmartVision Institute of Technology')
    short_name = db.Column(db.String(50), default='SmartVision')
    slogan = db.Column(db.String(255), default='Empowering Intelligence, Inspiring Academic Excellence')
    president_name = db.Column(db.String(100), default='Prof. S. K. Verma')
    dean_name = db.Column(db.String(100), default='Dr. R. Sharma')
    registrar_name = db.Column(db.String(100), default='Dr. A. K. Mishra')
    logo_filename = db.Column(db.String(255), nullable=True)
    logo_data = db.Column(db.Text, nullable=True) # Permanent base64 persistence in Neon DB
    name_image_filename = db.Column(db.String(255), nullable=True)
    name_image_data = db.Column(db.Text, nullable=True) # Permanent base64 persistence in Neon DB
    header_display_mode = db.Column(db.String(20), default='BOTH') # 'TEXT', 'IMAGE', 'BOTH'
    signature_filename = db.Column(db.String(255), nullable=True)
    signature_data = db.Column(db.Text, nullable=True) # Permanent base64 persistence in Neon DB
    address = db.Column(db.String(255), default='SmartVision Academic Campus, IT Knowledge Park, City')
    phone = db.Column(db.String(50), default='+91 98765 43210')
    email = db.Column(db.String(100), default='admin@smartvision.edu')
    website = db.Column(db.String(100), default='https://smartvision.edu')
    accreditation = db.Column(db.String(200), default='NAAC A++ Accredited | AICTE Approved')
    established_year = db.Column(db.String(20), default='2018')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get_settings(cls):
        """Helper to get or initialize default university settings."""
        settings = cls.query.first()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings


# ==============================================================================
# SECTION 9: FLASK-LOGIN USER LOADER CALLBACK
# ==============================================================================
@login_manager.user_loader
def load_user(user_id):
    """Flask-Login user loader retrieving User object by primary key."""
    return User.query.get(int(user_id))