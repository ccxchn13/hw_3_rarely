from __future__ import annotations

import secrets
import string
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib.auth import login
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_time
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Course, InstructorProfile, Lecture, QRCodeUpload, StudentProfile, decodeQR, getUploadsForCourse
CDT = ZoneInfo("America/Chicago")

# ---------------------------------------------------------------------------
# HW2  — index, time, sum
# ---------------------------------------------------------------------------

def time_view(request: HttpRequest) -> HttpResponse:
    """Return current Chicago time as HH:MM (24-hour), plain text."""
    now = datetime.now(tz=CDT)
    return HttpResponse(now.strftime("%H:%M"), content_type="text/plain")


def _decimal_to_str(x: Decimal) -> str:
    """Format Decimal without scientific notation and without trailing zeros."""
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def sum_view(request: HttpRequest) -> HttpResponse:
    """Return n1 + n2 from query params as plain text."""
    n1_str = request.GET.get("n1", "0")
    n2_str = request.GET.get("n2", "0")

    try:
        val = Decimal(n1_str) + Decimal(n2_str)
    except (InvalidOperation, ValueError):
        return HttpResponse("Bad Request: n1 and n2 must be numbers", status=400, content_type="text/plain")

    return HttpResponse(_decimal_to_str(val), content_type="text/plain")


# ---------------------------------------------------------------------------
# HW4  — user creation
# ---------------------------------------------------------------------------

def index(request):
    """Home page: app name, a short bio, the current Central time, and the logged-in user."""
    context = {
        "current_time": datetime.now(CDT).strftime("%H:%M"),
        "username": request.user.username if request.user.is_authenticated else None,
    }
    return render(request, "app/index.html", context)

@require_GET
def new(request):
    """Sign-up form. Only GET is allowed; any other method gets 405."""
    return render(request, "app/new.html")

@csrf_exempt
@require_POST
def createUser(request):
    """
    Create a Django User plus a StudentProfile or InstructorProfile, then log the user in.

    POST fields: user_name, email, password, is_student ("1" = student, "0" = instructor).
    Returns 200 on success, 400 for missing fields or a duplicate email or user name,
    and 405 for any method other than POST.
    """
    user_name = request.POST.get("user_name", "").strip()
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "")
    is_student = request.POST.get("is_student", "1").strip().lower() not in ("0", "false", "no", "instructor")

    if not user_name or not email or not password:
        return HttpResponseBadRequest("Missing user_name, email, or password.")
    if User.objects.filter(email__iexact=email).exists():
        return HttpResponseBadRequest("A user with this email already exists.")
    if User.objects.filter(username=user_name).exists():
        return HttpResponseBadRequest("This user name is already taken.")

    with transaction.atomic():
        user = User.objects.create_user(username=user_name, email=email, password=password)
        if is_student:
            StudentProfile.objects.create(user=user)
        else:
            InstructorProfile.objects.create(user=user)

    login(request, user)
    role = "student" if is_student else "instructor"
    return HttpResponse(f'Created {role} account for {user_name}. <a href="/app/">Go to the home page</a>')
    

# ---------------------------------------------------------------------------
# HW5  — courses, lectures, QR uploads, dump
# ---------------------------------------------------------------------------


# Form field name -> meeting_days letter
DAY_FIELDS = [
    ("day-mon", "M"),
    ("day-tue", "T"),
    ("day-wed", "W"),
    ("day-thu", "R"),
    ("day-fri", "F"),
]
 
 
def _instructor_of(request):
    """Return the logged-in user's InstructorProfile, or None if there isn't one."""
    if not request.user.is_authenticated:
        return None
    return InstructorProfile.objects.filter(user=request.user).first()
 
 
def _student_of(request):
    """Return the logged-in user's StudentProfile, or None if there isn't one."""
    if not request.user.is_authenticated:
        return None
    return StudentProfile.objects.filter(user=request.user).first()
 
 
def _unauthorized(role):
    """HTTP 401 response for a request from someone without the required role."""
    return HttpResponse(f"Unauthorized: you must be logged in as {role}.", status=401)
 
 
def _box_checked(request, field):
    """True if a checkbox field was sent (browsers send "on"), unless it says 0/false/off/no."""
    return field in request.POST and request.POST[field].strip().lower() not in ("0", "false", "off", "no")
 
 
@require_GET
def new_course(request):
    """Form for creating a course; submits to /app/createCourse."""
    return render(request, "app/new_course.html")
 
 
@require_GET
def new_lecture(request):
    """Form for creating a lecture; the dropdown lists every course by name."""
    courses = Course.objects.order_by("name")
    return render(request, "app/new_lecture.html", {"courses": courses})
 
 
@require_GET
def new_qr_upload(request):
    """Form for uploading a photo of a QR code; submits to /app/createQRCodeUpload."""
    return render(request, "app/new_qr_upload.html")
 
 
@csrf_exempt
@require_POST
def createCourse(request):
    """
    Create a Course taught by the logged-in instructor.
 
    POST fields: course-name, start-time, end-time (HH:MM), and one or more of
    day-mon ... day-fri. Returns 200 on success, 401 if the user is not an
    instructor, and 400 for missing or invalid fields.
    """
    instructor = _instructor_of(request)
    if instructor is None:
        return _unauthorized("an instructor")
 
    name = request.POST.get("course-name", "").strip()
    start_raw = request.POST.get("start-time", "").strip()
    end_raw = request.POST.get("end-time", "").strip()
    meeting_days = "".join(letter for field, letter in DAY_FIELDS if _box_checked(request, field))
 
    if not name or not start_raw or not end_raw or not meeting_days:
        return HttpResponseBadRequest(
            "Required: course-name, start-time, end-time, and at least one of day-mon ... day-fri."
        )
    try:
        start_time = parse_time(start_raw)
        end_time = parse_time(end_raw)
    except ValueError:
        start_time = end_time = None
    if start_time is None or end_time is None:
        return HttpResponseBadRequest("start-time and end-time must be times like 09:30.")
 
    course = Course.objects.create(
        name=name,
        instructor=instructor,
        meeting_days=meeting_days,
        start_time=start_time,
        end_time=end_time,
    )
    return HttpResponse(
        f"Created course {course.name} (meets {meeting_days}, "
        f"{start_time:%H:%M}-{end_time:%H:%M})."
    )
 
# Characters for generated QR identifiers: printable ASCII letters and digits.
QR_ALPHABET = string.ascii_letters + string.digits


def _random_qr_identifier():
    """Return 16 random letters and digits, using the secrets module so it can't be guessed."""
    return "".join(secrets.choice(QR_ALPHABET) for _ in range(16))
 
@csrf_exempt
@require_POST
def createLecture(request):
    """
    Create a Lecture for today in the course named by the POST field `choice`.
 
    The lecture's qr_identifier is the optional POST field `qrdata`, used exactly as
    sent; without it, 16 random letters and digits are generated. Returns 200 on
    success, 401 if the user is not an instructor, and 400 if the course is missing
    or unknown, or if another lecture already uses that QR identifier.
    """
    instructor = _instructor_of(request)
    if instructor is None:
        return _unauthorized("an instructor")
 
    course_name = request.POST.get("choice", "").strip()
    if not course_name:
        return HttpResponseBadRequest("Required: choice (a course name).")
    course = Course.objects.filter(name=course_name).order_by("-course_id").first()
    if course is None:
        return HttpResponseBadRequest(f"No course named {course_name!r}.")
 
    qrdata = request.POST.get("qrdata", "")
    qr_identifier = qrdata if qrdata else _random_qr_identifier()
    if Lecture.objects.filter(qr_identifier=qr_identifier).exists():
        return HttpResponseBadRequest(f"Another lecture already uses the QR identifier {qr_identifier!r}.")

    lecture = Lecture.objects.create(
        course=course,
        date=timezone.localdate(),
        qr_identifier=qr_identifier,
    )
    return HttpResponse(
        f"Created lecture for {course.name} on {lecture.date} "
        f"(QR identifier: {lecture.qr_identifier})."
    )

@csrf_exempt
@require_POST
def createQRCodeUpload(request):
    """
    Save a QR-code photo uploaded by the logged-in student and link it to its lecture.
 
    Expects a file field named imageUpload (the form must use
    enctype="multipart/form-data"). The student comes from the login and the
    time is set by the server. The photo is decoded with decodeQR and matched to
    the lecture whose qr_identifier equals the decoded text.
 
    Every upload is stored, valid or not, so invalid attempts stay on record:
      - QR code matches a lecture: stored with that lecture, HTTP 200.
      - No readable QR code, or no matching lecture: stored with no lecture,
        HTTP 400 {"error": "Invalid QR code"}.
    Returns 401 if the user is not a student (nothing is stored) and 400 if no
    file was sent.
    """
    student = _student_of(request)
    if student is None:
        return _unauthorized("a student")
 
    image = request.FILES.get("imageUpload")
    if image is None:
        return HttpResponseBadRequest(
            'Required: a file field named imageUpload (enctype="multipart/form-data").'
        )
    text = decodeQR(image)
    lecture = Lecture.objects.filter(qr_identifier=text).first() if text else None

    # Store the upload first, valid or not, so invalid attempts are still recorded.
    upload = QRCodeUpload.objects.create(student=student, lecture=lecture, image_uploaded=image)
    if lecture is None:
        return JsonResponse({"error": "Invalid QR code"}, status=400)

    return JsonResponse({
        "status": "ok",
        "upload_id": upload.upload_id,
        "course": lecture.course.name,
        "lecture_date": str(lecture.date),
        "upload_time": timezone.localtime(upload.time_uploaded, CDT).strftime("%Y-%m-%d %H:%M:%S"),
    }) 

 
@require_GET
def dumpUploads(request):
    """
    JSON list of every upload: [{"username": ..., "upload_time": "YYYY-MM-DD HH:MM:SS"}, ...].
 
    Times are in Central time. Returns 401 if the user is not an instructor.
    """
    if _instructor_of(request) is None:
        return _unauthorized("an instructor")
 
    uploads = QRCodeUpload.objects.select_related("student__user").order_by("time_uploaded")
    data = [
        {
            "username": upload.student.user.username,
            "upload_time": timezone.localtime(upload.time_uploaded).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for upload in uploads
    ]
    return JsonResponse(data, safe=False)

# ---------------------------------------------------------------------------
# HW6  — getUploads
# ---------------------------------------------------------------------------

@require_GET
def getUploads(request):
    """
    JSON list of the valid uploads for one course:
    [{"username": ..., "upload_time": "YYYY-MM-DD HH:MM:SS"}, ...].

    Query parameter: course, a course number, e.g. /app/getUploads?course=DATA13600.
    Only uploads made on a meeting day during class time are listed. Times are in Central time.
    Returns 401 if the user is not an instructor, 400 if course is missing,
    and an empty list if no course has that number.
    """
    if _instructor_of(request) is None:
        return _unauthorized("an instructor")

    course = request.GET.get("course", "").strip()
    if not course:
        return JsonResponse({"error": "Missing course parameter"}, status=400)

    data = [
        {
            "username": upload.student.user.username,
            "upload_time": timezone.localtime(upload.time_uploaded, CDT).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for upload in getUploadsForCourse(course)
    ]
    return JsonResponse(data, safe=False)