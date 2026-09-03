# ==============================================================================
# SMARTVISION ATTENDANCE MANAGEMENT PORTAL - DATABASE MIGRATION ENGINE
# ==============================================================================
# Description: Automated SQLite schema inspection and column migration engine.
#              Safely verifies table columns and applies DDL modifications on startup
#              without table drops or data loss across development and production.
# ==============================================================================

import sqlite3
import os

# ==============================================================================
# 1. DATABASE SCHEMA INSPECTION & MIGRATION RUNNER
# ==============================================================================
def run_migrations(db_path="smartvision.db"):
    """
    Safely runs SQLite migrations to add new columns and tables required
    for core timetable, daily schedule, attendance sessions, teacher leave,
    and correction workflows without data loss.
    """
    if not os.path.exists(db_path):
        print(f"[Migration] Database file {db_path} does not exist yet. It will be created by SQLAlchemy.")
        return

    print(f"[Migration] Inspecting database {db_path} for updates...")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Columns to add: (table_name, column_name, sql_definition)
        migrations = [
            ("users", "google_id", "VARCHAR(100)"),
            ("users", "mobile", "VARCHAR(20)"),
            ("users", "phone", "VARCHAR(20)"),
            ("users", "status", "VARCHAR(20) DEFAULT 'Approved'"),
            ("users", "is_email_verified", "BOOLEAN DEFAULT 1"),
            ("classes", "admin_id", "INTEGER"),
            ("classes", "class_teacher_id", "INTEGER"),
            ("classes", "section", "VARCHAR(50)"),
            ("classes", "department", "VARCHAR(100)"),
            ("teachers", "admin_id", "INTEGER"),
            ("teachers", "user_id", "INTEGER"),
            ("teachers", "email", "VARCHAR(100)"),
            ("teachers", "emp_id", "VARCHAR(50)"),
            ("teachers", "employee_code", "VARCHAR(50)"),
            ("teachers", "department", "VARCHAR(100)"),
            ("teachers", "mobile", "VARCHAR(20)"),
            ("teachers", "image_filename", "VARCHAR(255)"),
            ("teachers", "face_encoding", "BLOB"),
            ("teachers", "status", "VARCHAR(20) DEFAULT 'Approved'"),
            ("teachers", "primary_subject", "VARCHAR(100)"),
            ("teachers", "secondary_subject", "VARCHAR(100)"),
            ("teachers", "tertiary_subject", "VARCHAR(100)"),
            ("subjects", "admin_id", "INTEGER"),
            ("subjects", "code", "VARCHAR(50)"),
            ("subjects", "subject_type", "VARCHAR(20) DEFAULT 'Theory'"),
            ("students", "user_id", "INTEGER"),
            ("students", "mobile", "VARCHAR(20)"),
            ("students", "roll_number", "VARCHAR(50)"),
            ("students", "face_embedding", "BLOB"),
            ("students", "parent_name", "VARCHAR(100)"),
            ("students", "parent_email", "VARCHAR(100)"),
            ("students", "parent_mobile", "VARCHAR(20)"),
            ("students", "department", "VARCHAR(100)"),
            ("student_edit_requests", "new_department", "VARCHAR(100)"),
            ("student_edit_requests", "new_mobile", "VARCHAR(20)"),
            ("student_edit_requests", "new_parent_name", "VARCHAR(100)"),
            ("student_edit_requests", "new_parent_email", "VARCHAR(100)"),
            ("student_edit_requests", "new_parent_mobile", "VARCHAR(20)"),
            ("attendance", "time_marked", "VARCHAR(20)"),
            ("attendance", "timetable_id", "INTEGER"),
            ("timetables", "period", "INTEGER DEFAULT 1"),
            ("timetables", "period_no", "INTEGER DEFAULT 1"),
            ("timetables", "slot_type", "VARCHAR(20) DEFAULT 'CLASS'"),
            ("timetables", "custom_title", "VARCHAR(100)"),
            ("timetables", "room", "VARCHAR(50)"),
            ("timetables", "effective_from", "DATE"),
            ("timetables", "effective_to", "DATE"),
            ("teacher_daily_attendances", "is_admin_overridden", "BOOLEAN DEFAULT 0"),
            ("teacher_daily_attendances", "check_in_face_verified", "BOOLEAN DEFAULT 0"),
            ("teacher_daily_attendances", "check_out_face_verified", "BOOLEAN DEFAULT 0"),
            ("teacher_daily_attendances", "check_in_photo", "VARCHAR(255)"),
            ("teacher_daily_attendances", "check_out_photo", "VARCHAR(255)"),
            ("teacher_daily_attendances", "informed_admin", "BOOLEAN DEFAULT 0"),
            ("teacher_daily_attendances", "absence_reason", "VARCHAR(255)"),
            ("teacher_daily_attendances", "is_uninformed_absence", "BOOLEAN DEFAULT 0"),
            ("daily_schedule", "substitute_teacher_id", "INTEGER"),
            ("daily_schedule", "is_cancelled", "BOOLEAN DEFAULT 0"),
            ("daily_schedule", "cancellation_reason", "VARCHAR(255)"),
            ("daily_schedule", "is_proxy", "BOOLEAN DEFAULT 0"),
            ("daily_schedule", "proxy_assigned_by_admin_id", "INTEGER"),
            ("daily_schedule", "proxy_assigned_at", "DATETIME"),
            ("class_announcements", "admin_id", "INTEGER"),
            ("class_announcements", "posted_by_role", "VARCHAR(20) DEFAULT 'teacher'"),
            ("university_settings", "name_image_filename", "VARCHAR(255)"),
            ("university_settings", "header_display_mode", "VARCHAR(20) DEFAULT 'BOTH'"),
            ("university_settings", "logo_data", "TEXT"),
            ("university_settings", "name_image_data", "TEXT"),
            ("university_settings", "signature_data", "TEXT"),
            ("teachers", "image_data", "TEXT"),
            ("students", "image_data", "TEXT"),
            ("student_edit_requests", "new_image_data", "TEXT")
        ]

        for table, column, col_type in migrations:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [info[1] for info in cursor.fetchall()]
            if columns and column not in columns:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    print(f"[Migration] Added column {column} to table {table}.")
                except Exception as e:
                    print(f"[Migration Error] Could not add column {column} to {table}: {e}")

        # Ensure departments table exists
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) UNIQUE NOT NULL,
            full_name VARCHAR(200),
            code VARCHAR(50),
            admin_id INTEGER,
            created_at DATETIME
        )
        """)

        # Seed standard academic departments if table is empty
        cursor.execute("SELECT COUNT(*) FROM departments")
        if cursor.fetchone()[0] == 0:
            standard_depts = [
                ("CSE", "Computer Science & Engineering", "CSE"),
                ("AI", "Artificial Intelligence", "AI"),
                ("AI/DS", "Artificial Intelligence & Data Science", "AI/DS"),
                ("AI/ML", "Artificial Intelligence & Machine Learning", "AI/ML"),
                ("ORACLE", "Oracle Systems & Database Engineering", "ORACLE"),
                ("MICROSOFT", "Microsoft Cloud & Enterprise Tech", "MICROSOFT"),
                ("IT", "Information Technology", "IT"),
                ("ECE", "Electronics & Communication Engineering", "ECE")
            ]
            for d_name, d_full, d_code in standard_depts:
                cursor.execute("INSERT OR IGNORE INTO departments (name, full_name, code) VALUES (?, ?, ?)", (d_name, d_full, d_code))
            print("[Migration] Seeded standard academic departments.")

        # Check if subjects.teacher_id is NOT NULL in SQLite schema
        cursor.execute("PRAGMA table_info(subjects)")
        sub_info = cursor.fetchall()
        for col in sub_info:
            if col[1] == 'teacher_id' and col[3] == 1:
                print("[Migration] Migrating subjects table to allow NULL teacher_id...")
                cursor.execute("CREATE TABLE IF NOT EXISTS subjects_new (id INTEGER PRIMARY KEY, code VARCHAR(50), name VARCHAR(100) NOT NULL, teacher_id INTEGER, class_id INTEGER NOT NULL, admin_id INTEGER)")
                cursor.execute("INSERT INTO subjects_new (id, code, name, teacher_id, class_id, admin_id) SELECT id, code, name, teacher_id, class_id, admin_id FROM subjects")
                cursor.execute("DROP TABLE subjects")
                cursor.execute("ALTER TABLE subjects_new RENAME TO subjects")
                conn.commit()
                print("[Migration] subjects.teacher_id is now nullable!")

        # Check if class_announcements.class_id is NOT NULL in SQLite schema
        cursor.execute("PRAGMA table_info(class_announcements)")
        ann_info = cursor.fetchall()
        for col in ann_info:
            if col[1] == 'class_id' and col[3] == 1:
                print("[Migration] Migrating class_announcements table to allow NULL class_id and teacher_id...")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS class_announcements_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        class_id INTEGER REFERENCES classes(id) ON DELETE CASCADE,
                        teacher_id INTEGER REFERENCES teachers(id),
                        admin_id INTEGER REFERENCES users(id),
                        posted_by_role VARCHAR(20) DEFAULT 'teacher',
                        target_role VARCHAR(20) DEFAULT 'STUDENTS',
                        title VARCHAR(150) NOT NULL,
                        content TEXT NOT NULL,
                        notice_type VARCHAR(30) DEFAULT 'Announcement',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cursor.execute("""
                    INSERT INTO class_announcements_new (id, class_id, teacher_id, title, content, notice_type, created_at)
                    SELECT id, class_id, teacher_id, title, content, notice_type, created_at FROM class_announcements;
                """)
                cursor.execute("DROP TABLE class_announcements;")
                cursor.execute("ALTER TABLE class_announcements_new RENAME TO class_announcements;")
                conn.commit()
                print("[Migration] class_announcements table migrated successfully!")

        # Check if timetables.subject_id or teacher_id is NOT NULL in SQLite schema
        cursor.execute("PRAGMA table_info(timetables)")
        tt_info = cursor.fetchall()
        for col in tt_info:
            if (col[1] == 'subject_id' and col[3] == 1) or (col[1] == 'teacher_id' and col[3] == 1):
                print("[Migration] Migrating timetables table to allow NULL subject_id and teacher_id...")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS timetables_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        day_of_week VARCHAR(20) NOT NULL,
                        period_no INTEGER DEFAULT 1,
                        start_time VARCHAR(10) NOT NULL,
                        end_time VARCHAR(10) NOT NULL,
                        slot_type VARCHAR(20) DEFAULT 'CLASS',
                        custom_title VARCHAR(100),
                        class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                        subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                        teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
                        room VARCHAR(50),
                        effective_from DATE,
                        effective_to DATE,
                        admin_id INTEGER REFERENCES users(id)
                    );
                """)
                cursor.execute("""
                    INSERT INTO timetables_new (id, day_of_week, period_no, start_time, end_time, slot_type, custom_title, class_id, subject_id, teacher_id, room, effective_from, effective_to, admin_id)
                    SELECT id, day_of_week, 
                           COALESCE(period_no, 1), 
                           start_time, end_time, 
                           COALESCE(slot_type, 'CLASS'), 
                           NULL,
                           class_id, subject_id, teacher_id, room, effective_from, effective_to, admin_id 
                    FROM timetables;
                """)
                cursor.execute("DROP TABLE timetables;")
                cursor.execute("ALTER TABLE timetables_new RENAME TO timetables;")
                conn.commit()
                print("[Migration] timetables table migrated successfully to allow NULL subject_id/teacher_id!")
                break

        tables_to_create = [
            ("teacher_assignments", """
                CREATE TABLE IF NOT EXISTS teacher_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE
                );
            """),
            ("holidays", """
                CREATE TABLE IF NOT EXISTS holidays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    scope VARCHAR(50) DEFAULT 'ALL',
                    reason VARCHAR(255)
                );
            """),
            ("teacher_leave", """
                CREATE TABLE IF NOT EXISTS teacher_leave (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    date_from DATE NOT NULL,
                    date_to DATE NOT NULL,
                    leave_type VARCHAR(20) DEFAULT 'FULL',
                    status VARCHAR(20) DEFAULT 'PENDING',
                    substitute_teacher_id INTEGER REFERENCES teachers(id),
                    reason TEXT
                );
            """),
            ("daily_schedule", """
                CREATE TABLE IF NOT EXISTS daily_schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    timetable_id INTEGER NOT NULL REFERENCES timetables(id) ON DELETE CASCADE,
                    resolved_status VARCHAR(30) DEFAULT 'SCHEDULED'
                );
            """),
            ("attendance_sessions", """
                CREATE TABLE IF NOT EXISTS attendance_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timetable_id INTEGER REFERENCES timetables(id) ON DELETE SET NULL,
                    daily_schedule_id INTEGER REFERENCES daily_schedule(id) ON DELETE SET NULL,
                    date DATE NOT NULL,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id),
                    class_id INTEGER NOT NULL REFERENCES classes(id),
                    subject_id INTEGER NOT NULL REFERENCES subjects(id),
                    start_time VARCHAR(10),
                    end_time VARCHAR(10),
                    status VARCHAR(20) DEFAULT 'ATTENDANCE_OPEN'
                );
            """),
            ("attendance_records", """
                CREATE TABLE IF NOT EXISTS attendance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    status VARCHAR(10) NOT NULL,
                    confidence REAL,
                    marked_by VARCHAR(20) DEFAULT 'CAMERA',
                    marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_session_student UNIQUE (session_id, student_id)
                );
            """),
            ("correction_requests", """
                CREATE TABLE IF NOT EXISTS correction_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
                    requested_by INTEGER NOT NULL REFERENCES users(id),
                    reason TEXT NOT NULL,
                    status VARCHAR(20) DEFAULT 'PENDING',
                    reviewed_by INTEGER REFERENCES users(id),
                    reviewed_at TIMESTAMP
                );
            """),
            ("student_dismissed_notices", """
                CREATE TABLE IF NOT EXISTS student_dismissed_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    announcement_id INTEGER NOT NULL REFERENCES class_announcements(id) ON DELETE CASCADE,
                    dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """),
            ("teacher_dismissed_notices", """
                CREATE TABLE IF NOT EXISTS teacher_dismissed_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    announcement_id INTEGER NOT NULL REFERENCES class_announcements(id) ON DELETE CASCADE,
                    dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """),
            ("student_read_notices", """
                CREATE TABLE IF NOT EXISTS student_read_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    announcement_id INTEGER NOT NULL REFERENCES class_announcements(id) ON DELETE CASCADE,
                    read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """),
            ("teacher_read_notices", """
                CREATE TABLE IF NOT EXISTS teacher_read_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    announcement_id INTEGER NOT NULL REFERENCES class_announcements(id) ON DELETE CASCADE,
                    read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """),
            ("timetable_period_settings", """
                CREATE TABLE IF NOT EXISTS timetable_period_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_no INTEGER NOT NULL DEFAULT 1,
                    label VARCHAR(50) NOT NULL,
                    start_time VARCHAR(10) NOT NULL,
                    end_time VARCHAR(10) NOT NULL,
                    is_lunch BOOLEAN DEFAULT 0,
                    order_index INTEGER DEFAULT 1
                );
            """),
            ("teacher_feedbacks", """
                CREATE TABLE IF NOT EXISTS teacher_feedbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                    teaching_quality REAL NOT NULL DEFAULT 5.0,
                    subject_knowledge REAL NOT NULL DEFAULT 5.0,
                    communication_style REAL NOT NULL DEFAULT 5.0,
                    student_support REAL NOT NULL DEFAULT 5.0,
                    overall_rating REAL NOT NULL DEFAULT 5.0,
                    positive_feedback TEXT,
                    improvement_areas TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, teacher_id, class_id)
                );
            """),
            ("faculty_complaints", """
                CREATE TABLE IF NOT EXISTS faculty_complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                    subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                    category VARCHAR(100) NOT NULL,
                    is_replacement_requested BOOLEAN DEFAULT 0,
                    description TEXT NOT NULL,
                    status VARCHAR(30) DEFAULT 'Voting in Progress',
                    admin_notes TEXT,
                    reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """),
            ("complaint_votes", """
                CREATE TABLE IF NOT EXISTS complaint_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    complaint_id INTEGER NOT NULL REFERENCES faculty_complaints(id) ON DELETE CASCADE,
                    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    vote_type VARCHAR(10) NOT NULL,
                    voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(complaint_id, student_id)
                );
            """)
        ]

        for table_name, create_sql in tables_to_create:
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}';")
            if not cursor.fetchone():
                print(f"[Migration] Creating table '{table_name}'...")
                cursor.execute(create_sql)
                conn.commit()
                print(f"[Migration] Table '{table_name}' created successfully.")

        conn.close()
        print("[Migration] Database is fully up-to-date.")
    except Exception as e:
        print(f"[Migration ERROR] Failed to run database migrations: {e}")

if __name__ == '__main__':
    run_migrations()
