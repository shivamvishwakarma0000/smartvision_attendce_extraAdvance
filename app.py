# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - CORE APPLICATION ENTRY POINT
# ==============================================================================
# Description: Main application factory, extension initializations, modular blueprint
#              registrations, Jinja template filters, context processors, and startup seeding.
# ==============================================================================

import os
from flask import Flask
from config import Config
from extensions import db, login_manager, oauth
try:
    import face_recognition
except ImportError:
    face_recognition = None
import db_migrations

# ==============================================================================
# APPLICATION FACTORY FUNCTION
# ==============================================================================
def create_app():
    """
    Constructs and configures the Flask core application instance.
    Initializes database ORM, user session authentication, OAuth providers,
    modular blueprint routes, Jinja filters, context processors, and migrations.
    """
    app = Flask(__name__)
    app.config.from_object(Config)

    # --------------------------------------------------------------------------
    # 1. INITIALIZE FLASK EXTENSIONS
    # --------------------------------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    oauth.init_app(app)

    # --------------------------------------------------------------------------
    # 2. GOOGLE OAUTH 2.0 CLIENT REGISTRATION
    # --------------------------------------------------------------------------
    if app.config.get('GOOGLE_CLIENT_ID') and app.config.get('GOOGLE_CLIENT_SECRET'):
        oauth.register(
            name='google',
            client_id=app.config['GOOGLE_CLIENT_ID'],
            client_secret=app.config['GOOGLE_CLIENT_SECRET'],
            access_token_url='https://oauth2.googleapis.com/token',
            access_token_params=None,
            authorize_url='https://accounts.google.com/o/oauth2/v2/auth',
            authorize_params=None,
            api_base_url='https://www.googleapis.com/oauth2/v2/',
            client_kwargs={'scope': 'openid email profile'},
            server_metadata_url='https://accounts.google.com/.well-known/openid-configuration'
        )

    # --------------------------------------------------------------------------
    # 3. REGISTER APPLICATION BLUEPRINTS (MODULAR ROUTING)
    # --------------------------------------------------------------------------
    from auth.routes import auth_bp
    from main.routes import main_bp
    from student.routes import student_bp
    from teacher.routes import teacher_bp
    from teacher_attendance.routes import teacher_attendance_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(teacher_attendance_bp)

    # --------------------------------------------------------------------------
    # 4. LIGHTWEIGHT HEALTH MONITORING (UPTIMEROBOT 24/7 KEEP-ALIVE)
    # --------------------------------------------------------------------------
    @app.route('/health')
    @app.route('/api/health')
    def health_check():
        """Ultra-lightweight heartbeat endpoint for UptimeRobot to keep Render instance awake 24/7."""
        import datetime
        return {
            'status': 'online',
            'service': 'SmartVision Attendance Portal',
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
        }, 200

    # --------------------------------------------------------------------------
    # 4. CUSTOM JINJA TEMPLATE FILTERS
    # --------------------------------------------------------------------------
    @app.template_filter('to_ist')
    def to_ist_filter(dt, fmt="%b %d, %Y %I:%M %p"):
        """Formats UTC datetimes into Indian Standard Time (UTC + 5:30) for display."""
        if not dt:
            return "N/A"
        try:
            import datetime
            if isinstance(dt, datetime.datetime):
                ist_dt = dt + datetime.timedelta(hours=5, minutes=30)
                return ist_dt.strftime(fmt)
            return str(dt)
        except Exception:
            return str(dt)

    # --------------------------------------------------------------------------
    # 5. GLOBAL CONTEXT PROCESSORS (LIVE BADGES & NOTIFICATION COUNTERS)
    # --------------------------------------------------------------------------
    @app.context_processor
    def inject_teacher_notifications():
        """Injects pending proxy duties, attendance transfers, and unread notices for teachers."""
        try:
            from flask_login import current_user
            if current_user.is_authenticated and current_user.role == 'teacher':
                teacher = getattr(current_user, 'teacher_profile', None)
                if teacher:
                    from models import AttendanceSession, AttendanceDiscrepancyRequest, Subject
                    teacher_subjects = Subject.query.filter_by(teacher_id=teacher.id).all()
                    subject_ids = [s.id for s in teacher_subjects]
                    
                    teacher_sessions = AttendanceSession.query.filter(
                        (AttendanceSession.teacher_id == teacher.id) |
                        (AttendanceSession.subject_id.in_(subject_ids) if subject_ids else False)
                    ).all()
                    session_ids = [s.id for s in teacher_sessions]

                    filter_conds = []
                    if session_ids:
                        filter_conds.append(AttendanceDiscrepancyRequest.session_id.in_(session_ids))
                    if teacher.name:
                        filter_conds.append(AttendanceDiscrepancyRequest.teacher_name.ilike(f"%{teacher.name}%"))

                    from models import TeacherLeave, Timetable, ProxyAttendanceTransfer
                    from extensions import get_current_date
                    today = get_current_date()

                    # Count today's proxy duty slots that have NOT been shared yet
                    proxy_leaves = TeacherLeave.query.filter_by(
                        substitute_teacher_id=teacher.id,
                        status='APPROVED'
                    ).all()

                    # Collect all (timetable_id, date) pairs already shared
                    sent_shared = ProxyAttendanceTransfer.query.filter_by(
                        substitute_teacher_id=teacher.id,
                        status='SHARED'
                    ).all()
                    shared_keys = set((t.timetable_id, t.date) for t in sent_shared if t.timetable_id and t.date)

                    proxy_count = 0
                    for l in proxy_leaves:
                        if l.date_from <= today <= l.date_to:
                            orig = l.teacher
                            if not orig:
                                continue
                            day_name = today.strftime('%A')
                            slots = Timetable.query.filter_by(
                                teacher_id=orig.id,
                                day_of_week=day_name,
                                slot_type='CLASS'
                            ).all()
                            for slot in slots:
                                if (slot.id, today) not in shared_keys:
                                    proxy_count += 1

                    # Count direct emergency DailySchedule proxy allocations
                    from models import DailySchedule
                    direct_proxy_today = DailySchedule.query.filter_by(
                        substitute_teacher_id=teacher.id,
                        date=today,
                        is_proxy=True
                    ).all()
                    for dp in direct_proxy_today:
                        if not dp.is_cancelled and (dp.timetable_id, today) not in shared_keys:
                            if dp.timetable and dp.timetable.teacher_id not in [l.teacher_id for l in proxy_leaves if l.teacher_id]:
                                proxy_count += 1

                    count = 0
                    if filter_conds:
                        from sqlalchemy import or_
                        count = AttendanceDiscrepancyRequest.query.filter(
                            AttendanceDiscrepancyRequest.status == 'PENDING',
                            or_(*filter_conds)
                        ).count()

                    received_transfers = ProxyAttendanceTransfer.query.filter(
                        ProxyAttendanceTransfer.original_teacher_id == teacher.id,
                        ProxyAttendanceTransfer.status.in_(['PENDING', 'SHARED'])
                    ).order_by(ProxyAttendanceTransfer.created_at.desc()).all()

                    from models import ClassAnnouncement, TeacherDismissedNotice, TeacherReadNotice
                    dismissed_ids = [d.announcement_id for d in TeacherDismissedNotice.query.filter_by(teacher_id=teacher.id).all()]
                    read_ids = [r.announcement_id for r in TeacherReadNotice.query.filter_by(teacher_id=teacher.id).all()]
                    excluded_ids = set(dismissed_ids + read_ids)
                    q_notice = ClassAnnouncement.query.filter(
                        ClassAnnouncement.posted_by_role == 'admin',
                        ClassAnnouncement.target_role.in_(['TEACHERS', 'ALL'])
                    )
                    if excluded_ids:
                        q_notice = q_notice.filter(~ClassAnnouncement.id.in_(list(excluded_ids)))
                    teacher_notice_count = q_notice.count()

                    return {
                        'pending_discrepancies_count': count,
                        'proxy_classes_count': proxy_count,
                        'received_proxy_transfers': received_transfers,
                        'received_proxy_transfers_count': len(received_transfers),
                        'teacher_notice_count': teacher_notice_count
                    }
        except Exception as e:
            print(f"Error in inject_teacher_notifications: {e}")
        return {'pending_discrepancies_count': 0, 'proxy_classes_count': 0, 'received_proxy_transfers': [], 'received_proxy_transfers_count': 0, 'teacher_notice_count': 0}

    @app.context_processor
    def inject_student_notifications():
        """Injects unread announcements and notices counter for student portals."""
        try:
            from flask_login import current_user
            if current_user.is_authenticated and current_user.role == 'student':
                student = getattr(current_user, 'student_profile', None)
                if not student:
                    from models import Student
                    student = Student.query.filter_by(user_id=current_user.id).first()
                if student:
                    from models import ClassAnnouncement, StudentDismissedNotice, StudentReadNotice
                    from sqlalchemy import or_
                    dismissed_ids = [d.announcement_id for d in StudentDismissedNotice.query.filter_by(student_id=student.id).all()]
                    read_ids = [r.announcement_id for r in StudentReadNotice.query.filter_by(student_id=student.id).all()]
                    excluded_ids = set(dismissed_ids + read_ids)
                    q = ClassAnnouncement.query.filter(
                        ClassAnnouncement.target_role.in_(['STUDENTS', 'ALL']),
                        or_(ClassAnnouncement.class_id == None, ClassAnnouncement.class_id == student.class_id)
                    )
                    if excluded_ids:
                        q = q.filter(~ClassAnnouncement.id.in_(list(excluded_ids)))
                    return {'student_notice_count': q.count()}
        except Exception as e:
            print(f"Error in inject_student_notifications: {e}")
        return {'student_notice_count': 0}

    @app.context_processor
    def inject_admin_notifications():
        """Injects pending leave applications and student profile correction counts for admin."""
        try:
            from flask_login import current_user
            if current_user.is_authenticated and current_user.role == 'admin':
                from models import TeacherLeave, StudentEditRequest
                pending_leaves = TeacherLeave.query.filter_by(status='PENDING').count()
                pending_corrections = StudentEditRequest.query.filter_by(status='Pending').count()
                return {
                    'admin_pending_leaves_count': pending_leaves,
                    'admin_pending_corrections_count': pending_corrections,
                    'admin_pending_approvals_count': pending_leaves + pending_corrections
                }
        except Exception as e:
            print(f"Error in inject_admin_notifications: {e}")
        return {
            'admin_pending_leaves_count': 0,
            'admin_pending_corrections_count': 0,
            'admin_pending_approvals_count': 0
        }

    @app.context_processor
    def inject_university_settings():
        """Injects institutional profile, branding, logos, leadership names everywhere."""
        try:
            from models import UniversitySettings
            settings = UniversitySettings.get_settings()
            return {'university': settings}
        except Exception as e:
            print(f"Error in inject_university_settings: {e}")
            return {'university': None}


    # --------------------------------------------------------------------------
    # 6. DATABASE SCHEMA MIGRATION & SEEDING ON STARTUP
    # --------------------------------------------------------------------------
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///smartvision.db')
    if db_uri.startswith('sqlite:///'):
        db_path = db_uri.replace('sqlite:///', '')
        if not db_path.startswith('/'):
            db_path = os.path.join(app.instance_path, db_path)
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        db_migrations.run_migrations(db_path)

    with app.app_context():
        db.create_all()
        setup_initial_data()

    # Enable reverse proxy support for secure HTTPS OAuth redirects on Render / AWS / Railway
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    except Exception:
        pass

    return app

# ==============================================================================
# INITIAL DATA SEEDING FUNCTION
# ==============================================================================
def setup_initial_data():
    """Seeds default administrator accounts if not present in the database."""
    from models import User
    admin_shivam = User.query.filter_by(email='admin@smart.com').first()
    if not admin_shivam:
        admin_shivam = User(name='Admin Shivam', email='admin@smart.com', role='admin', status='Approved')
        admin_shivam.set_password('@Shivam0000')
        db.session.add(admin_shivam)
        print("[Database Seed] Admin user created: admin@smart.com / @Shivam0000")

    admin_def = User.query.filter_by(email='admin@smartvision.com').first()
    if not admin_def:
        admin_def = User(name='Admin', email='admin@smartvision.com', role='admin', status='Approved')
        admin_def.set_password('password123')
        db.session.add(admin_def)
        print("[Database Seed] Admin user created: admin@smartvision.com / password123")
    
    db.session.commit()

# ==============================================================================
# APPLICATION INSTANCE CREATION
# ==============================================================================
app = create_app()

# ==============================================================================
# LOCAL DEVELOPMENT SERVER EXECUTION
# ==============================================================================
if __name__ == '__main__':
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 7860))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug_mode, use_reloader=False)