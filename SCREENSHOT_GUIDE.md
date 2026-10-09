# 📸 Screenshot Guide & Visual Capture Checklist
## Selfie Smart Vision Attendance Portal — Parul University (Final Year Project)

This checklist tells you **exactly where, how, and with what accounts** to take each of the 12 screenshots required for your updated project report.

---

### 🚀 Step 1: Start Your Portal Locally
Run your Flask server in your terminal:
```bash
python app.py
```
Default URL: `http://127.0.0.1:5000`

---

### 📋 Step-by-Step Screenshot Checklist

| Figure # | Screenshot Name | Source URL / Route | Login Account / Role | What to Show on Screen |
| :---: | :--- | :--- | :--- | :--- |
| **Figure 6.10** | **Public Landing Page & Branding** | `http://127.0.0.1:5000/` | *No login needed* (Public) | Navbar with university logo & NAAC A++ badge, hero headline, quick login buttons, and feature cards. |
| **Figure 6.11** | **Unified Multi-Role Login Portal** | `http://127.0.0.1:5000/login` | *No login needed* (Public) | Login card showing the role tabs (**Admin**, **Teacher**, **Student**), email & password fields, and Google SSO button. |
| **Figure 6.12** | **Master Admin Analytics Dashboard** | `http://127.0.0.1:5000/admin/dashboard` | **Admin** (`admin@smartvision.edu`) | Total Students card, Active Faculty card, Daily Presence % widget, Retention Risk card, and Chart.js graphs. |
| **Figure 6.13** | **AI Facial Attendance Scanner** | `http://127.0.0.1:5000/take_attendance` | **Teacher** or **Admin** | Class/Subject selectors, classroom group photo with green/red bounding boxes, recognized student names, and presence table. |
| **Figure 6.14** | **Geofenced Faculty GPS Check-In** | `http://127.0.0.1:5000/teacher/my_attendance` | **Teacher** | GPS live coordinates, distance badge (*"Within 50m geofence"*), selfie webcam preview, check-in & check-out time logs. |
| **Figure 6.15** | **Master Timetable & Daily Scheduling** | `http://127.0.0.1:5000/manage_timetable` | **Admin** | Weekly recurring timetable grid (Mon–Sat, Periods 1–8) with subjects, assigned teachers, room numbers, and lunch/break slots. |
| **Figure 6.16** | **Emergency Proxy Desk & Recommender** | `http://127.0.0.1:5000/admin/emergency_proxy_desk` | **Admin** | Unassigned class lecture slots due to absent faculty, candidate substitute teacher recommendation list, and *"Assign Proxy"* buttons. |
| **Figure 6.17** | **Digital Student ID Cards & Suspension** | `http://127.0.0.1:5000/admin/id_cards` | **Admin** | High-fidelity digital ID card with student photo, enrollment info, dynamic scannable QR code, status badge, and suspension reason selector. |
| **Figure 6.18** | **Student Academic Dashboard** | `http://127.0.0.1:5000/student/dashboard` | **Student** | Overall attendance percentage radial gauge, subject-wise progress bars (green for $\ge 75\%$, red for $<75\%$), and today's lecture schedule. |
| **Figure 6.19** | **Anonymous Teacher Feedback & Rating** | `http://127.0.0.1:5000/student/feedback` | **Student** | 4-dimensional 5-star rating sliders (Teaching Quality, Subject Knowledge, Communication, Student Support), comment boxes, and anonymity notice. |
| **Figure 6.20** | **Exam Eligibility Calculator & Export** | `http://127.0.0.1:5000/export_exam_eligibility` | **Admin** | Table of student attendance percentages, eligibility status (*Eligible / Condonation / Detained*), and the **Export to Excel / CSV** buttons. |
| **Figure 6.21** | **Enterprise AI Copilot Assistant** | Click Floating Widget on any page | **Any Role** (e.g. Student / Teacher) | Chat drawer showing conversational query (e.g. *"What is my attendance in Machine Learning?"*), formatted markdown answer, and suggestion chips. |

---

### 💡 Pro Tips for Capturing High-Score Screenshots
1. **Zoom Level:** Set browser zoom to 90% or 100% so full card borders and headers are visible in one screen.
2. **Mac Shortcut:** Press `Cmd + Shift + 4` to select the exact window area or clean rectangular component.
3. **Data Polish:** Ensure clean test data (e.g., sample classes like *7CSE4*, subjects like *Machine Learning*, *Computer Networks*) is populated before snapping.
