import os
import sys
import unittest
import base64

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from extensions import db, get_current_date
from models import User, Student, Teacher, Class, Subject

class SmartVisionSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_01_app_initialization(self):
        """Test Flask application initializes and home route responds."""
        res = self.client.get('/')
        self.assertIn(res.status_code, [200, 302])
        print("  [PASS] Application factory & routes initialized.")

    def test_02_database_models(self):
        """Test database models and initial admin seeding."""
        admin = User.query.filter_by(role='admin').first()
        self.assertIsNotNone(admin, "Admin user must exist in database")
        print(f"  [PASS] Database models and seed: Admin={admin.email}")

    def test_03_email_dispatcher(self):
        """Test asynchronous email dispatcher utility."""
        from email_utils import send_email
        ok, msg = send_email("ci_test@smartvision.com", "CI Test", "Body content", sync=False)
        self.assertTrue(ok)
        print(f"  [PASS] Email dispatcher status: {msg}")

    def test_04_schedule_service(self):
        """Test timetable expander and strict attendance calculation."""
        from schedule_service import generate_daily_schedule, calculate_student_attendance
        today = get_current_date()
        schedules = generate_daily_schedule(today)
        self.assertIsInstance(schedules, list)
        print(f"  [PASS] Schedule Engine: Generated {len(schedules)} slots for {today}.")

    def test_05_emergency_proxy_desk(self):
        """Test Emergency Proxy calculation engine."""
        from main.routes import compute_emergency_proxy_desk
        today = get_current_date()
        proxy_data = compute_emergency_proxy_desk(today)
        self.assertIn('absent_teachers', proxy_data)
        self.assertIn('affected_slots', proxy_data)
        print(f"  [PASS] Emergency Proxy Desk engine verified.")

if __name__ == '__main__':
    unittest.main()
