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
    # PWA (PROGRESSIVE WEB APP) ROOT ENDPOINTS
    # --------------------------------------------------------------------------
    @app.route('/manifest.json')
    def serve_manifest():
        """Serves web app manifest at root scope for PWA installation & TWA/PWABuilder."""
        from flask import send_from_directory
        return send_from_directory(os.path.join(app.root_path, 'static'), 'manifest.json', mimetype='application/manifest+json')

    @app.route('/sw.js')
    def serve_service_worker():
        """Serves service worker from root to give it maximum scope over the entire application."""
        from flask import send_from_directory
        return send_from_directory(os.path.join(app.root_path, 'static'), 'sw.js', mimetype='application/javascript')

    @app.route('/download-apk')
    @app.route('/download/apk')
    def download_apk():
        """Serves downloadable SmartVision Android APK file."""
        from flask import send_from_directory
        return send_from_directory(os.path.join(app.root_path, 'static', 'downloads'), 'SmartVision.apk', as_attachment=True, download_name='SmartVision.apk')


    # --------------------------------------------------------------------------
    # 4. CUSTOM JINJA TEMPLATE FILTERS (INDIAN STANDARD TIME CONVERSION)
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

    @app.template_filter('to_ist_time')
    def to_ist_time_filter(dt, fmt="%I:%M %p"):
        """Formats UTC datetimes into accurate Indian Standard Time (IST) 12-hour clock (e.g. '01:28 PM')."""
        if not dt:
            return "Recorded"
        try:
            import datetime
            if isinstance(dt, datetime.datetime):
                ist_dt = dt + datetime.timedelta(hours=5, minutes=30)
                return ist_dt.strftime(fmt)
            return str(dt)
        except Exception:
            return str(dt)

    @app.template_filter('to_ist_date')
    def to_ist_date_filter(dt, fmt="%b %d, %Y"):
        """Formats UTC datetimes/dates into accurate Indian Standard Time (IST) date string."""
        if not dt:
            return "N/A"
        try:
            import datetime
            if isinstance(dt, datetime.datetime):
                ist_dt = dt + datetime.timedelta(hours=5, minutes=30)
                return ist_dt.strftime(fmt)
            elif isinstance(dt, datetime.date):
                return dt.strftime(fmt)
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

                    from models import TeacherLeave, Timetable, ProxyAttendanceTransfer, AttendanceSession, DailySchedule
                    from extensions import get_current_date
                    today = get_current_date()

                    # Collect all (timetable_id, date) pairs that are already shared, applied, dismissed, or completed
                    sent_transfers = ProxyAttendanceTransfer.query.filter(
                        ProxyAttendanceTransfer.substitute_teacher_id == teacher.id,
                        ProxyAttendanceTransfer.status.in_(['SHARED', 'APPLIED', 'DISMISSED'])
                    ).all()
                    shared_keys = set((t.timetable_id, t.date) for t in sent_transfers if t.timetable_id and t.date)

                    # Also include completed sessions taken by this substitute teacher
                    completed_sessions = AttendanceSession.query.filter_by(
                        teacher_id=teacher.id,
                        status='COMPLETED'
                    ).all()
                    for cs in completed_sessions:
                        if cs.timetable_id and cs.date:
                            shared_keys.add((cs.timetable_id, cs.date))

                    # Count unshared proxy duty slots assigned to this teacher
                    proxy_leaves = TeacherLeave.query.filter_by(
                        substitute_teacher_id=teacher.id,
                        status='APPROVED'
                    ).all()

                    proxy_count = 0
                    for l in proxy_leaves:
                        orig = l.teacher
                        if not orig:
                            continue
                        curr = l.date_from
                        while curr <= l.date_to:
                            day_name = curr.strftime('%A')
                            slots = Timetable.query.filter_by(
                                teacher_id=orig.id,
                                day_of_week=day_name,
                                slot_type='CLASS'
                            ).all()
                            for slot in slots:
                                if (slot.id, curr) not in shared_keys:
                                    proxy_count += 1
                            curr += timedelta(days=1)

                    # Count direct emergency DailySchedule proxy allocations
                    direct_proxies = DailySchedule.query.filter_by(
                        substitute_teacher_id=teacher.id,
                        is_proxy=True
                    ).all()
                    for dp in direct_proxies:
                        if not dp.is_cancelled and (dp.timetable_id, dp.date) not in shared_keys:
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
        """Injects pending leave applications, student profile corrections, and suspension removal request counts for admin."""
        try:
            from flask_login import current_user
            if current_user.is_authenticated and current_user.role == 'admin':
                from models import TeacherLeave, StudentEditRequest, TeacherEditRequest, Teacher, SuspensionRemovalRequest
                pending_leaves = TeacherLeave.query.filter_by(status='PENDING').count()
                pending_corrections = StudentEditRequest.query.filter_by(status='Pending').count()
                pending_teacher_edits = TeacherEditRequest.query.filter_by(status='Pending').count()
                pending_teachers = Teacher.query.filter_by(status='Pending').count()
                pending_suspension_requests = SuspensionRemovalRequest.query.filter_by(status='Pending').count()
                return {
                    'admin_pending_leaves_count': pending_leaves,
                    'admin_pending_corrections_count': pending_corrections,
                    'admin_pending_teacher_edits_count': pending_teacher_edits,
                    'admin_pending_approvals_count': pending_leaves + pending_corrections + pending_teacher_edits + pending_teachers,
                    'pending_suspension_requests_count': pending_suspension_requests
                }
        except Exception as e:
            print(f"Error in inject_admin_notifications: {e}")
        return {
            'admin_pending_leaves_count': 0,
            'admin_pending_corrections_count': 0,
            'admin_pending_approvals_count': 0,
            'pending_suspension_requests_count': 0
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

    @app.before_request
    def auto_lift_expired_detentions():
        """Automatically checks and releases completed student and teacher detentions across the portal."""
        if request.endpoint and not request.endpoint.startswith('static'):
            try:
                from models import check_and_auto_lift_detentions
                check_and_auto_lift_detentions()
            except Exception:
                pass


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
        try:
            db.create_all()
            ensure_postgresql_columns()
            setup_initial_data()
            sync_university_images_to_db()
        except Exception as startup_db_err:
            print(f"[Warning] Database startup synchronization failed (will retry on incoming request): {startup_db_err}")

    # Enable reverse proxy support for secure HTTPS OAuth redirects on Render / AWS / Railway
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    except Exception:
        pass

    @app.errorhandler(413)
    def request_entity_too_large(e):
        flash("The uploaded photo/file exceeds the allowable limit. Please select a compressed photo or capture a live webcam photo.", "warning")
        return redirect(request.referrer or url_for('main.index', state='register'))

    @app.errorhandler(500)
    def internal_server_error(e):
        import traceback
        print("\n" + "=" * 80)
        print("[CRITICAL 500 INTERNAL SERVER ERROR TRACEBACK]")
        traceback.print_exc()
        print("=" * 80 + "\n", flush=True)
        try:
            db.session.rollback()
        except Exception:
            pass
        flash("A server error occurred. Your session has been safely reset. Please try signing in.", "danger")
        return redirect(url_for('main.index', state='login'))

    return app

# ==============================================================================
# INITIAL DATA SEEDING & ASSET SYNC FUNCTIONS
# ==============================================================================
def ensure_postgresql_columns():
    """Runs idempotent column check on Neon PostgreSQL / SQLite for all schema columns."""
    try:
        from sqlalchemy import text
        statements = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mobile VARCHAR(20)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS parent_name VARCHAR(100)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS parent_email VARCHAR(100)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS parent_mobile VARCHAR(20)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS image_data TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS face_embedding BYTEA",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS face_encoding BYTEA",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS image_data TEXT",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS primary_subject VARCHAR(100)",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS secondary_subject VARCHAR(100)",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS tertiary_subject VARCHAR(100)",
            "ALTER TABLE subjects ADD COLUMN IF NOT EXISTS code VARCHAR(50)",
            "ALTER TABLE subjects ADD COLUMN IF NOT EXISTS subject_type VARCHAR(20) DEFAULT 'Theory'",
            "ALTER TABLE student_edit_requests ADD COLUMN IF NOT EXISTS new_parent_name VARCHAR(100)",
            "ALTER TABLE student_edit_requests ADD COLUMN IF NOT EXISTS new_parent_email VARCHAR(100)",
            "ALTER TABLE student_edit_requests ADD COLUMN IF NOT EXISTS new_parent_mobile VARCHAR(20)",
            "ALTER TABLE student_edit_requests ADD COLUMN IF NOT EXISTS new_image_data TEXT",
            "ALTER TABLE student_edit_requests ADD COLUMN IF NOT EXISTS new_face_encoding BYTEA",
            "ALTER TABLE university_settings ADD COLUMN IF NOT EXISTS logo_data TEXT",
            "ALTER TABLE university_settings ADD COLUMN IF NOT EXISTS name_image_data TEXT",
            "ALTER TABLE university_settings ADD COLUMN IF NOT EXISTS signature_data TEXT",
            """CREATE TABLE IF NOT EXISTS teacher_feedbacks (
                id SERIAL PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                teaching_quality DOUBLE PRECISION NOT NULL DEFAULT 5.0,
                subject_knowledge DOUBLE PRECISION NOT NULL DEFAULT 5.0,
                communication_style DOUBLE PRECISION NOT NULL DEFAULT 5.0,
                student_support DOUBLE PRECISION NOT NULL DEFAULT 5.0,
                overall_rating DOUBLE PRECISION NOT NULL DEFAULT 5.0,
                positive_feedback TEXT,
                improvement_areas TEXT,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_student_teacher_class_feedback UNIQUE(student_id, teacher_id, class_id)
            )""",
            """CREATE TABLE IF NOT EXISTS faculty_complaints (
                id SERIAL PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                category VARCHAR(100) NOT NULL,
                is_replacement_requested BOOLEAN DEFAULT FALSE,
                description TEXT NOT NULL,
                status VARCHAR(30) DEFAULT 'Voting in Progress',
                admin_notes TEXT,
                reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS complaint_votes (
                id SERIAL PRIMARY KEY,
                complaint_id INTEGER NOT NULL REFERENCES faculty_complaints(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                vote_type VARCHAR(10) NOT NULL,
                voted_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_complaint_student_vote UNIQUE(complaint_id, student_id)
            )""",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS id_card_status VARCHAR(30) DEFAULT 'Active'",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN DEFAULT FALSE",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS suspension_reason VARCHAR(255)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS custom_suspension_reason TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMP WITHOUT TIME ZONE",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS suspended_by_user_id INTEGER",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS suspended_by_role VARCHAR(50)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS suspended_by_name VARCHAR(100)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS detention_days INTEGER",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS suspended_until TIMESTAMP WITHOUT TIME ZONE",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS id_card_status VARCHAR(30) DEFAULT 'Active'",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN DEFAULT FALSE",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS suspension_reason VARCHAR(255)",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS custom_suspension_reason TEXT",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMP WITHOUT TIME ZONE",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS suspended_by_user_id INTEGER",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS suspended_by_role VARCHAR(50)",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS suspended_by_name VARCHAR(100)",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS detention_days INTEGER",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS suspended_until TIMESTAMP WITHOUT TIME ZONE",
            """CREATE TABLE IF NOT EXISTS suspension_audits (
                id SERIAL PRIMARY KEY,
                target_type VARCHAR(20) NOT NULL,
                student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                teacher_id INTEGER REFERENCES teachers(id) ON DELETE CASCADE,
                action VARCHAR(30) NOT NULL,
                reason VARCHAR(255),
                custom_reason TEXT,
                performed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                performed_by_role VARCHAR(50),
                performed_by_name VARCHAR(100),
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS suspension_removal_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                target_type VARCHAR(20) NOT NULL,
                student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                teacher_id INTEGER REFERENCES teachers(id) ON DELETE CASCADE,
                explanation TEXT NOT NULL,
                supporting_document VARCHAR(255),
                additional_comments TEXT,
                status VARCHAR(30) DEFAULT 'Pending',
                admin_notes TEXT,
                reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                reviewed_at TIMESTAMP WITHOUT TIME ZONE,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )"""
        ]
        for stmt in statements:
            try:
                db.session.execute(text(stmt))
                db.session.commit()
            except Exception:
                db.session.rollback()
        try:
            db.create_all()
        except Exception:
            pass
        print("[PostgreSQL Schema Sync] Verified and updated all database columns successfully!")
    except Exception as e:
        print(f"[PostgreSQL Schema Sync Notice] {e}")

def sync_university_images_to_db():
    """Ensures university logos and banners are permanently stored as Base64 in Neon PostgreSQL."""
    try:
        import base64
        from models import UniversitySettings
        settings = UniversitySettings.get_settings()
        changed = False

        static_univ_dir = os.path.join(os.getcwd(), 'static', 'images', 'university')

        # 1. Sync Logo
        if not settings.logo_data:
            logo_src = os.path.join(static_univ_dir, 'parul_logo.png')
            if not os.path.exists(logo_src):
                logo_src = os.path.join(static_univ_dir, 'univ_logo_1788059444.png')
            if os.path.exists(logo_src):
                with open(logo_src, 'rb') as f:
                    settings.logo_data = base64.b64encode(f.read()).decode('utf-8')
                    if not settings.logo_filename:
                        settings.logo_filename = 'parul_logo.png'
                    changed = True

        # 2. Sync Wordmark Name Banner
        if not settings.name_image_data:
            name_src = os.path.join(static_univ_dir, 'parul_name.png')
            if not os.path.exists(name_src):
                name_src = os.path.join(static_univ_dir, 'univ_name_1788060474.png')
            if os.path.exists(name_src):
                with open(name_src, 'rb') as f:
                    settings.name_image_data = base64.b64encode(f.read()).decode('utf-8')
                    if not settings.name_image_filename:
                        settings.name_image_filename = 'parul_name.png'
                    changed = True

        # 3. Sync Signature Stamp
        if not settings.signature_data:
            sig_src = os.path.join(static_univ_dir, 'parul_signature.png')
            if not os.path.exists(sig_src):
                sig_src = os.path.join(static_univ_dir, 'univ_signature_1788059444.png')
            if os.path.exists(sig_src):
                with open(sig_src, 'rb') as f:
                    settings.signature_data = base64.b64encode(f.read()).decode('utf-8')
                    if not settings.signature_filename:
                        settings.signature_filename = 'parul_signature.png'
                    changed = True

        if changed:
            db.session.commit()
            print("[University Sync] Successfully stored permanent Base64 university assets into Neon PostgreSQL DB!")
    except Exception as e:
        print(f"[University Sync Notice] {e}")

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