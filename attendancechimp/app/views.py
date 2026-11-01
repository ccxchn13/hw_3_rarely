from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib.auth import login
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import InstructorProfile, StudentProfile

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



# ---------------------------------------------------------------------------
# HW6  — getUploads
# ---------------------------------------------------------------------------

