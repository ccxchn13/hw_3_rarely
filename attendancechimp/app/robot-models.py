
# LLM: Claude Code Opus 5.5 Extra
# Prompt: Write a complete Django models.py for an attendance-tracking app called AttendanceChimp.
# There are two user roles, students and instructors, implemented as InstructorProfile and StudentProfile tables linked to Django’s built-in User. 
# Each Course has one instructor and a meeting schedule; students can enroll in many courses through an Enrollment table. 
# A Lecture is one class meeting of a course, with a date and a unique QR identifier. 
# A QRCodeUpload is a photo uploaded by a student, linked to the student and optionally to a lecture. 
# The design should be simple to use, support attendance analytics, and be robust to cheating. Include docstrings.
"""
AttendanceChimp data model (``app/models.py``).

AttendanceChimp replaces paper sign-in sheets with QR codes:

1. An instructor opens today's session for a course. The app creates a
   :class:`Lecture` with a unique, unguessable QR token and renders it as a
   QR code.
2. The QR code is projected in the classroom.
3. Each student photographs it and uploads the photo. The upload form has a
   single field (the photo), because the lecture is identified by the QR code
   itself.
4. The app decodes the photo, finds the lecture whose token it contains, runs
   the anti-cheating checks below and marks the upload *accepted* or
   *rejected*. An accepted :class:`QRCodeUpload` **is** the attendance record,
   so there is no separate attendance table that could drift out of sync.

Entity overview::

    auth.User ──1:1── InstructorProfile ──1:N── Course ──1:N── Lecture
    auth.User ──1:1── StudentProfile                            │
    StudentProfile ──N:M (through Enrollment)── Course          │
    StudentProfile ──1:N── QRCodeUpload ──N:1 (optional)────────┘

How the design requirements are met
-----------------------------------
Simple to use
    Students upload one photo and nothing else. Instructors call
    :meth:`Course.start_lecture` (one button) to get today's QR code.
    Views need a single entry point for uploads, :meth:`QRCodeUpload.submit`,
    which returns the processed upload together with a message the student
    can read.

Attendance analytics
    ``Lecture.objects.with_attendance()`` annotates per-session attendance
    counts in one query (for the instructor dashboard).
    :meth:`Course.attendance_summary` and :meth:`Course.attendance_json`
    produce a JSON export with per-lecture and per-student figures.

Robust to cheating
    * **Duplicate or forged codes.** Tokens are 192-bit random values from
      :mod:`secrets` and are unique across all lectures, so a code can't be
      guessed, derived from the course and date, or reused for another
      session. A leaked code can be invalidated with
      :meth:`Lecture.rotate_qr_token`.
    * **Duplicate submissions.** A conditional unique constraint allows at
      most one *accepted* upload per student per lecture, even when two
      requests arrive at the same time.
    * **Shared photos.** Every image is SHA-256 hashed, and a byte-identical
      photo that a *different* student already submitted is rejected.
    * **Time and day.** An upload is accepted only if its server-side
      timestamp falls inside the course's scheduled meeting window on a
      scheduled meeting day, using the course's own time zone. Data the
      client controls (EXIF timestamps, form fields) is never used for
      timing.
    * **Audit trail.** Rejected uploads are kept with a machine-readable
      reason, so instructors can review suspicious activity.

Requirements
------------
* Django 5.1 or newer (this file uses ``CheckConstraint(condition=...)``; on
  older versions rename ``condition=`` to ``check=``), with ``USE_TZ = True``.
* Pillow (required by ``ImageField``), plus ``pyzbar`` and the zbar system
  library (``brew install zbar`` / ``apt install libzbar0``) for decoding.
* SQLite or PostgreSQL. MySQL silently ignores conditional unique
  constraints; the application-level duplicate check still applies there.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import PurePath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files import File
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MaxValueValidator, validate_image_file_extension
from django.db import IntegrityError, models, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: One-letter weekday codes in ``date.weekday()`` order (Monday == 0), using
#: the registrar convention ``R`` = Thursday and ``U`` = Sunday. A
#: Monday/Wednesday/Friday course therefore has ``meeting_days == "MWF"``.
WEEKDAY_CODES = "MTWRFSU"

#: Text that every AttendanceChimp QR code starts with. The prefix lets the
#: decoder ignore unrelated QR codes that also appear in the photo (for
#: example, one printed on a lecture slide).
QR_PAYLOAD_PREFIX = "attendancechimp:"

#: Largest photo accepted, in bytes. Phone photos are typically 2-6 MB.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: Signature of a QR decoder: takes an open image file and returns the text of
#: every QR code found in it (an empty list if there are none).
QRDecoder = Callable[[File], list[str]]


# ---------------------------------------------------------------------------
# Field defaults, validators and helpers
# ---------------------------------------------------------------------------


def generate_qr_token() -> str:
    """
    Return a new random, URL-safe token that identifies one lecture.

    ``secrets.token_urlsafe(24)`` returns 32 characters carrying 192 bits of
    cryptographic randomness. Because of that, tokens can't be predicted from
    the course or date, and accidental collisions are practically impossible.
    The unique constraint on :attr:`Lecture.qr_token` is the final guarantee.

    This is a module-level function (not a lambda) so migrations can serialize
    it as a field default.
    """
    return secrets.token_urlsafe(24)


def default_course_timezone() -> str:
    """Default a new course's time zone to the project's ``TIME_ZONE`` setting."""
    return settings.TIME_ZONE


def validate_meeting_days(value: str) -> None:
    """
    Validate a meeting-day string such as ``"MWF"`` or ``"TR"``.

    The check is case-insensitive. It rejects empty strings, letters outside
    :data:`WEEKDAY_CODES` and repeated days.

    Raises:
        ValidationError: if *value* isn't a valid set of meeting days.
    """
    codes = (value or "").upper()
    if not codes:
        raise ValidationError("Choose at least one meeting day.")
    invalid = sorted(set(codes) - set(WEEKDAY_CODES))
    if invalid:
        raise ValidationError(
            "Unknown day code(s) %(codes)s. Use M T W R F S U "
            "(R = Thursday, U = Sunday).",
            params={"codes": ", ".join(invalid)},
        )
    if len(set(codes)) != len(codes):
        raise ValidationError("Each meeting day may appear only once.")


def validate_timezone_name(value: str) -> None:
    """
    Validate that *value* is an IANA time-zone name such as ``"America/Chicago"``.

    Raises:
        ValidationError: if :class:`zoneinfo.ZoneInfo` can't load the zone.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            "%(value)s is not a valid time zone (expected e.g. America/Chicago).",
            params={"value": value},
        ) from None


def validate_upload_size(image: File) -> None:
    """
    Reject uploads larger than :data:`MAX_UPLOAD_BYTES`.

    Raises:
        ValidationError: if the file is too large.
    """
    if image.size > MAX_UPLOAD_BYTES:
        raise ValidationError(
            "Photo is too large (%(size).1f MB). The limit is %(limit)d MB.",
            params={
                "size": image.size / 1024 / 1024,
                "limit": MAX_UPLOAD_BYTES // 1024 // 1024,
            },
        )


def qr_upload_path(instance: QRCodeUpload, filename: str) -> str:
    """
    Build the storage path for an uploaded photo: ``qr_uploads/<student pk>/<uuid>.<ext>``.

    The original filename is discarded. It is controlled by the client, might
    contain personal information, and could collide with another file. Only a
    short alphanumeric extension is kept, so the stored file still opens in
    image viewers.
    """
    suffix = PurePath(filename).suffix.lower()
    if not (suffix[1:].isalnum() and len(suffix) <= 6):
        suffix = ""
    return f"qr_uploads/{instance.student_id}/{uuid.uuid4().hex}{suffix}"


def sha256_of_file(file: File) -> str:
    """Return the hex SHA-256 digest of *file*, read in chunks to bound memory use."""
    digest = hashlib.sha256()
    for chunk in file.chunks():
        digest.update(chunk)
    return digest.hexdigest()


def decode_qr_payloads(image_file: File) -> list[str]:
    """
    Return the text of every QR code found in *image_file*, using Pillow and pyzbar.

    This is the default decoder for :meth:`QRCodeUpload.process`. You can pass
    any other callable with the same signature instead (for example, a stub
    in unit tests, or an OpenCV-based decoder). zbar finds QR codes at any
    rotation, so phone photos don't need EXIF-orientation correction.

    Returns:
        The decoded strings, in the order zbar reports them. Returns an empty
        list if the photo contains no QR code or isn't a readable image.

    Raises:
        ImproperlyConfigured: if Pillow, pyzbar or the zbar system library is
            missing. A server misconfiguration shouldn't be reported to the
            student as a bad photo, so this error is not caught here.
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import ZBarSymbol, decode
    except ImportError as exc:  # pyzbar also raises ImportError when libzbar is missing
        raise ImproperlyConfigured(
            "QR decoding needs Pillow and pyzbar (`pip install Pillow pyzbar`) "
            "plus the zbar library (`brew install zbar` or `apt install libzbar0`)."
        ) from exc

    image_file.seek(0)
    try:
        with Image.open(image_file) as img:
            symbols = decode(img, symbols=[ZBarSymbol.QRCODE])
    except (OSError, Image.DecompressionBombError):
        return []
    return [symbol.data.decode("utf-8", errors="replace") for symbol in symbols]


def _normalize_meeting_days(value: str) -> str:
    """Return *value* upper-cased, de-duplicated and in calendar order (``"wm"`` -> ``"MW"``)."""
    letters = set((value or "").upper())
    return "".join(code for code in WEEKDAY_CODES if code in letters)


def _rate(numerator: int, denominator: int) -> float | None:
    """Return ``numerator / denominator`` rounded to 3 places, or ``None`` if the denominator is 0."""
    return round(numerator / denominator, 3) if denominator else None


def _display_name(user) -> str:
    """Return a user's full name, falling back to their username."""
    return user.get_full_name() or user.get_username()


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class InstructorProfile(models.Model):
    """
    An instructor: someone who can create courses, open lectures and view
    attendance analytics.

    AttendanceChimp uses Django's built-in ``User`` for authentication
    (password hashing, sessions, ``@login_required``) and stores role-specific
    data in two profile tables instead of one wide users table with a role
    column. This has three benefits:

    * Role checks are explicit and cheap:
      ``hasattr(request.user, "instructor_profile")``.
    * Foreign keys enforce the role. :attr:`Course.instructor` can only point
      at an instructor, and :attr:`QRCodeUpload.student` only at a student.
    * Someone who is both (for example, a TA who also takes classes) simply
      has both profiles attached to one login.

    Attributes:
        user: The login account (one-to-one). Deleting the user deletes the
            profile, but this is blocked while the instructor still has
            courses (see :attr:`Course.instructor`).
        department: Optional academic department, used for display.
        created_at: When the profile was created.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instructor_profile",
    )
    department = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "instructor profile"

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        """Full name if set, otherwise username."""
        return _display_name(self.user)


class StudentProfile(models.Model):
    """
    A student: someone who can be enrolled in courses and upload QR photos.

    See :class:`InstructorProfile` for why roles are separate profile tables.

    Attributes:
        user: The login account (one-to-one).
        university_id: The ID number the institution issued. It is unique, so
            exports can be joined with registrar or gradebook data.
        created_at: When the profile was created.

    Reverse relations:
        enrollments: :class:`Enrollment` rows for this student.
        courses: Courses the student is or was enrolled in (through
            :class:`Enrollment`). Filter on ``enrollments__is_active`` for
            current courses only.
        uploads: Every :class:`QRCodeUpload` this student submitted, accepted
            or not.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="student_profile",
    )
    university_id = models.CharField(
        "university ID",
        max_length=32,
        unique=True,
        help_text="Student ID number issued by the institution.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "student profile"

    def __str__(self) -> str:
        return f"{self.display_name} ({self.university_id})"

    @property
    def display_name(self) -> str:
        """Full name if set, otherwise username."""
        return _display_name(self.user)

    def is_enrolled_in(self, course: Course) -> bool:
        """Return ``True`` if this student has an *active* enrollment in *course*."""
        return self.enrollments.filter(course=course, is_active=True).exists()

    def attended_lectures(self, course: Course | None = None) -> models.QuerySet[Lecture]:
        """
        Return the lectures this student has an accepted check-in for.

        Args:
            course: If given, only return lectures of that course.
        """
        lectures = Lecture.objects.filter(
            uploads__student=self, uploads__status=QRCodeUpload.Status.ACCEPTED
        )
        if course is not None:
            lectures = lectures.filter(course=course)
        return lectures.distinct()


# ---------------------------------------------------------------------------
# Courses and enrollment
# ---------------------------------------------------------------------------


class Course(models.Model):
    """
    A course taught by exactly one instructor on a fixed weekly schedule.

    Meeting schedule
        The course meets on ``meeting_days`` (for example ``"MWF"``) from
        ``start_time`` to ``end_time``, on every such day between
        ``start_date`` and ``end_date`` inclusive. Times are local wall-clock
        times in ``time_zone``. They are compared with upload timestamps only
        after conversion, so they behave correctly regardless of the server's
        time zone or daylight-saving changes. Classes that cross midnight
        aren't supported (a database constraint requires
        ``end_time > start_time``).

    Check-in window (anti-cheating)
        An upload for a lecture on day *D* is accepted only if it arrives
        between::

            D + start_time - checkin_opens_minutes_before
            D + end_time   + checkin_closes_minutes_after

        Both offsets are set per course, so each instructor can decide how
        strict to be. The defaults (open 10 minutes early, close when class
        ends) suit most lectures.

    Attributes:
        name: Human-readable title, for example "Introduction to Data Engineering".
        code: Optional catalog number, for example "DATA 13600".
        instructor: The single instructor who owns the course. ``PROTECT``
            stops an instructor from being deleted while their courses, and
            therefore attendance history, still exist.
        students: Many-to-many relation to students through :class:`Enrollment`.
        meeting_days: Weekday codes from :data:`WEEKDAY_CODES`, stored
            upper-case in calendar order.
        start_time / end_time: Daily meeting time (local to ``time_zone``).
        start_date / end_date: First and last day of the term (inclusive).
        time_zone: IANA zone name used to interpret the schedule.
        checkin_opens_minutes_before: How early check-in opens.
        checkin_closes_minutes_after: How long after class check-in stays open.
        created_at: When the course was created.
    """

    name = models.CharField(max_length=200)
    code = models.CharField(
        max_length=32, blank=True, help_text='Catalog number, e.g. "DATA 13600".'
    )
    instructor = models.ForeignKey(
        InstructorProfile, on_delete=models.PROTECT, related_name="courses"
    )
    students = models.ManyToManyField(
        StudentProfile, through="Enrollment", related_name="courses", blank=True
    )

    # -- Meeting schedule ---------------------------------------------------
    meeting_days = models.CharField(
        max_length=7,
        validators=[validate_meeting_days],
        help_text="Weekday letters, e.g. MWF or TR (R = Thursday, U = Sunday).",
    )
    start_time = models.TimeField(help_text="Class start time (local to the course's time zone).")
    end_time = models.TimeField(help_text="Class end time (local to the course's time zone).")
    start_date = models.DateField(help_text="First day of the term.")
    end_date = models.DateField(help_text="Last day of the term (inclusive).")
    time_zone = models.CharField(
        max_length=64,
        default=default_course_timezone,
        validators=[validate_timezone_name],
        help_text="IANA time zone the schedule is in, e.g. America/Chicago.",
    )

    # -- Check-in window policy --------------------------------------------
    checkin_opens_minutes_before = models.PositiveSmallIntegerField(
        default=10,
        validators=[MaxValueValidator(180)],
        help_text="Accept uploads this many minutes before class starts.",
    )
    checkin_closes_minutes_after = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(180)],
        help_text="Keep accepting uploads this many minutes after class ends.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date", "name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="ac_course_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")),
                name="ac_course_term_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}".strip()

    # -- Validation ---------------------------------------------------------

    def clean(self) -> None:
        """
        Normalize ``meeting_days`` and check that the time and date ranges are ordered.

        ModelForms and the admin call this through ``full_clean()``. The
        database check constraints enforce the same ordering rules for code
        that writes through the ORM directly.
        """
        super().clean()
        self.meeting_days = _normalize_meeting_days(self.meeting_days)
        errors = {}
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            errors["end_time"] = "Class must end after it starts (same day)."
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "Term can't end before it starts."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs) -> None:
        """Store ``meeting_days`` in canonical form (``"wm"`` becomes ``"MW"``) before saving."""
        self.meeting_days = _normalize_meeting_days(self.meeting_days)
        super().save(*args, **kwargs)

    # -- Schedule helpers -----------------------------------------------------

    @property
    def tzinfo(self) -> ZoneInfo:
        """The course's time zone as a :class:`zoneinfo.ZoneInfo`."""
        return ZoneInfo(self.time_zone)

    @property
    def meeting_weekdays(self) -> set[int]:
        """Meeting days as ``date.weekday()`` numbers, e.g. ``"MWF"`` -> ``{0, 2, 4}``."""
        return {
            WEEKDAY_CODES.index(code)
            for code in self.meeting_days.upper()
            if code in WEEKDAY_CODES
        }

    def local_today(self) -> date:
        """Today's date in the course's time zone, which may differ from the server's."""
        return timezone.localdate(timezone=self.tzinfo)

    def meets_on(self, day: date) -> bool:
        """Return ``True`` if *day* is a scheduled meeting day within the term."""
        return (
            self.start_date <= day <= self.end_date
            and day.weekday() in self.meeting_weekdays
        )

    def checkin_window(self, day: date) -> tuple[datetime, datetime]:
        """
        Return the time-zone-aware ``(opens, closes)`` check-in window for *day*.

        The window covers the scheduled class time on *day*, widened by
        ``checkin_opens_minutes_before`` and ``checkin_closes_minutes_after``.
        This method doesn't check whether the course meets on *day*; use
        :meth:`meets_on` (or :meth:`Lecture.is_checkin_open`) for that.
        """
        tz = self.tzinfo
        opens = datetime.combine(day, self.start_time, tzinfo=tz) - timedelta(
            minutes=self.checkin_opens_minutes_before
        )
        closes = datetime.combine(day, self.end_time, tzinfo=tz) + timedelta(
            minutes=self.checkin_closes_minutes_after
        )
        return opens, closes

    # -- Instructor actions -------------------------------------------------

    def start_lecture(self, day: date | None = None) -> Lecture:
        """
        Return the lecture for *day*, creating it (with a fresh QR token) if needed.

        This is the one-click "Start today's session" action. Calling it again
        on the same day returns the same lecture and QR code instead of
        creating a duplicate session.

        Args:
            day: Session date. Defaults to today in the course's time zone.

        Raises:
            ValidationError: if the course doesn't meet on *day*.
        """
        day = day or self.local_today()
        if not self.meets_on(day):
            raise ValidationError(
                f"{self} does not meet on {day:%A, %B %d, %Y} "
                f"(meets {self.meeting_days}, {self.start_date} to {self.end_date})."
            )
        lecture, _ = Lecture.objects.get_or_create(course=self, date=day)
        return lecture

    def enroll(self, student: StudentProfile) -> Enrollment:
        """
        Enroll *student* in this course, reactivating a previous enrollment if one exists.

        Returns:
            The (possibly pre-existing) :class:`Enrollment`.
        """
        enrollment, created = Enrollment.objects.get_or_create(student=student, course=self)
        if not created and not enrollment.is_active:
            enrollment.is_active = True
            enrollment.save(update_fields=["is_active"])
        return enrollment

    def active_enrollments(self) -> models.QuerySet[Enrollment]:
        """Enrollments of students who haven't dropped the course."""
        return self.enrollments.filter(is_active=True)

    def has_active_student(self, student: StudentProfile) -> bool:
        """Return ``True`` if *student* is currently enrolled in this course."""
        return self.active_enrollments().filter(student=student).exists()

    # -- Analytics and export -----------------------------------------------

    def to_dict(self) -> dict:
        """Return basic course metadata as a JSON-serializable dict (use with ``DjangoJSONEncoder``)."""
        return {
            "id": self.pk,
            "code": self.code,
            "name": self.name,
            "instructor": self.instructor.display_name,
            "meeting_days": self.meeting_days,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "time_zone": self.time_zone,
        }

    def attendance_summary(self, include_students_per_lecture: bool = False) -> dict:
        """
        Build the attendance report for this course as a plain dict.

        The report contains:

        * ``course``: metadata from :meth:`to_dict`.
        * ``generated_at``: when the report was built (UTC).
        * ``enrolled_students``: number of currently active enrollments.
        * ``lectures_held``: lectures dated today or earlier (course time zone).
        * ``lectures``: one entry per lecture (see :meth:`Lecture.to_dict`)
          with ``present``, ``enrolled`` and ``attendance_rate``.
        * ``students``: one entry per active student with
          ``lectures_attended`` and ``attendance_rate``, where the rate is
          ``lectures_attended / lectures_held``.

        Rates are fractions between 0 and 1, or ``None`` when the denominator
        is 0. They are measured against *current* enrollment, so students who
        dropped don't lower the rate, and students who joined late count
        against lectures held before they enrolled.

        Per-lecture counts take one query in total. With
        ``include_students_per_lecture=True``, each lecture also lists who
        checked in and when, at the cost of one extra query per lecture.
        """
        lectures = list(self.lectures.with_attendance().order_by("date"))
        enrolled = self.active_enrollments().count()
        today = self.local_today()
        lectures_held = sum(1 for lecture in lectures if lecture.date <= today)

        students = (
            StudentProfile.objects.filter(enrollments__course=self, enrollments__is_active=True)
            .select_related("user")
            .annotate(
                lectures_attended=Count(
                    "uploads",
                    filter=Q(
                        uploads__status=QRCodeUpload.Status.ACCEPTED,
                        uploads__lecture__course=self,
                    ),
                    distinct=True,
                )
            )
            .order_by("university_id")
        )

        return {
            "course": self.to_dict(),
            "generated_at": timezone.now(),
            "enrolled_students": enrolled,
            "lectures_held": lectures_held,
            "lectures": [
                lecture.to_dict(enrolled=enrolled, include_students=include_students_per_lecture)
                for lecture in lectures
            ],
            "students": [
                {
                    "university_id": student.university_id,
                    "name": student.display_name,
                    "lectures_attended": student.lectures_attended,
                    "attendance_rate": _rate(student.lectures_attended, lectures_held),
                }
                for student in students
            ],
        }

    def attendance_json(self, include_students_per_lecture: bool = False, **json_kwargs) -> str:
        """
        Return :meth:`attendance_summary` serialized as a JSON string.

        Dates, times and datetimes are written as ISO 8601 strings by
        :class:`~django.core.serializers.json.DjangoJSONEncoder`. Extra
        keyword arguments go to :func:`json.dumps` (``indent`` defaults to 2).
        A view can return this directly, for example:
        ``HttpResponse(course.attendance_json(), content_type="application/json")``.
        """
        json_kwargs.setdefault("indent", 2)
        return json.dumps(
            self.attendance_summary(include_students_per_lecture),
            cls=DjangoJSONEncoder,
            **json_kwargs,
        )


class Enrollment(models.Model):
    """
    Links a student to a course: the explicit "through" table of
    ``Course.students``.

    An explicit model is used instead of Django's automatic many-to-many
    table so that each link can carry its own data:

    * ``is_active``: dropping a course sets this to ``False`` instead of
      deleting the row, so past attendance still has context and the
      student can be reactivated with :meth:`Course.enroll`. Only active
      students may check in.
    * ``enrolled_at``: when the student joined, for auditing late adds.

    A database constraint ensures a student has at most one enrollment row
    per course.
    """

    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="enrollments"
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="enrollments")
    enrolled_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(
        default=True, help_text="Uncheck when the student drops; history is kept."
    )

    class Meta:
        ordering = ["course", "student__university_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course"], name="ac_one_enrollment_per_student_per_course"
            ),
        ]

    def __str__(self) -> str:
        status = "" if self.is_active else " (dropped)"
        return f"{self.student} in {self.course}{status}"


# ---------------------------------------------------------------------------
# Lectures
# ---------------------------------------------------------------------------


class LectureQuerySet(models.QuerySet):
    """Query helpers for :class:`Lecture`, available as ``Lecture.objects.<method>()``."""

    def with_attendance(self) -> LectureQuerySet:
        """
        Annotate each lecture with ``present_count``, the number of distinct students present.

        The instructor dashboard uses this to get per-session counts in a
        single query, for example
        ``Lecture.objects.for_instructor(me).with_attendance()``.
        :attr:`Lecture.attendance_count` reads the annotation when it exists.
        """
        return self.annotate(
            present_count=Count(
                "uploads__student",
                filter=Q(uploads__status=QRCodeUpload.Status.ACCEPTED),
                distinct=True,
            )
        )

    def for_instructor(self, instructor: InstructorProfile) -> LectureQuerySet:
        """Lectures of every course taught by *instructor*."""
        return self.filter(course__instructor=instructor)


class Lecture(models.Model):
    """
    One class session of a course, with its own QR code.

    Instructors normally create lectures through :meth:`Course.start_lecture`.
    The QR code shown in class encodes :attr:`qr_payload`, which is
    :data:`QR_PAYLOAD_PREFIX` followed by :attr:`qr_token`. A view can render
    it with, for example, the ``qrcode`` package:
    ``qrcode.make(lecture.qr_payload)``.

    Anti-cheating properties:

    * ``qr_token`` is random (see :func:`generate_qr_token`), can't be edited
      in forms or the admin, and is unique across *all* lectures. A code
      from one session or course is therefore never valid for another.
    * A course has at most one lecture per date (database constraint), so
      there is never more than one valid code for the same session.
    * Uploads are accepted only inside :meth:`checkin_window`, and only if
      the date is a scheduled meeting day (:meth:`is_checkin_open`).
    * If a photo of the code leaks (for example to a group chat), call
      :meth:`rotate_qr_token` and re-project. Check-ins already accepted
      still count, and uploads of the old code are rejected.

    Attributes:
        course: The course this session belongs to. Deleting the course
            deletes its lectures and their uploads.
        date: Calendar date of the session in the course's time zone. It
            must be a meeting day within the term (checked by :meth:`clean`
            and :meth:`is_checkin_open`).
        qr_token: The lecture's unique QR identifier.
        created_at: When the session was opened.

    Reverse relations:
        uploads: Every :class:`QRCodeUpload` matched to this lecture. The
            accepted ones are the attendance records.
    """

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="lectures")
    date = models.DateField(help_text="Session date, in the course's time zone.")
    qr_token = models.CharField(
        "QR identifier",
        max_length=64,
        unique=True,
        default=generate_qr_token,
        editable=False,
        help_text="Random token encoded in this session's QR code.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = LectureQuerySet.as_manager()

    class Meta:
        ordering = ["date", "course_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "date"], name="ac_one_lecture_per_course_per_day"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.course} on {self.date:%a %Y-%m-%d}"

    def clean(self) -> None:
        """
        Reject lectures scheduled on a day the course doesn't meet.

        Raises:
            ValidationError: if ``date`` isn't one of the course's meeting
                days within its term.
        """
        super().clean()
        if self.course_id and self.date and not self.course.meets_on(self.date):
            raise ValidationError(
                {
                    "date": (
                        f"{self.course} does not meet on {self.date:%A, %B %d, %Y} "
                        f"(meets {self.course.meeting_days}, "
                        f"{self.course.start_date} to {self.course.end_date})."
                    )
                }
            )

    # -- QR code --------------------------------------------------------------

    @property
    def qr_payload(self) -> str:
        """The exact text to encode in this lecture's QR code."""
        return f"{QR_PAYLOAD_PREFIX}{self.qr_token}"

    @staticmethod
    def token_from_payload(payload: str) -> str | None:
        """
        Extract the lecture token from decoded QR text.

        Returns:
            The token, or ``None`` if *payload* isn't an AttendanceChimp code
            (for example, a QR code printed on a slide that points to a
            website).
        """
        payload = (payload or "").strip()
        if not payload.startswith(QR_PAYLOAD_PREFIX):
            return None
        return payload[len(QR_PAYLOAD_PREFIX):] or None

    def rotate_qr_token(self) -> str:
        """
        Replace this lecture's QR token with a new random one and save.

        Use this when a photo of the code may have been shared with students
        who aren't in the room. Accepted check-ins are unaffected, and any
        later upload of the old code is rejected as ``UNKNOWN_CODE``.

        Returns:
            The new token.
        """
        self.qr_token = generate_qr_token()
        self.save(update_fields=["qr_token"])
        return self.qr_token

    # -- Time validation ----------------------------------------------------

    def checkin_window(self) -> tuple[datetime, datetime]:
        """Return the time-zone-aware ``(opens, closes)`` window for this session."""
        return self.course.checkin_window(self.date)

    def is_checkin_open(self, at: datetime | None = None) -> bool:
        """
        Return ``True`` if a check-in at *at* would be on time for this lecture.

        A check-in is on time when both of these hold:

        1. The lecture's date is a scheduled meeting day within the term. This
           is checked again here even though :meth:`clean` validates it,
           because rows created through the ORM skip ``clean()``.
        2. *at* falls inside :meth:`checkin_window` (bounds inclusive).

        Args:
            at: The moment to test. Defaults to now. When validating an
                upload, pass the upload's server-side ``uploaded_at`` so a
                delayed or retried processing job judges the time the photo
                was actually received.
        """
        at = at or timezone.now()
        if timezone.is_naive(at):
            at = timezone.make_aware(at)
        if not self.course.meets_on(self.date):
            return False
        opens, closes = self.checkin_window()
        return opens <= at <= closes

    # -- Analytics ----------------------------------------------------------

    @property
    def attendance_count(self) -> int:
        """
        Number of distinct students with an accepted check-in for this lecture.

        Uses the ``present_count`` annotation from
        :meth:`LectureQuerySet.with_attendance` when present, so looping over
        an annotated queryset doesn't run one query per lecture.
        """
        annotated = getattr(self, "present_count", None)
        if annotated is not None:
            return annotated
        return self.uploads.accepted().values("student").distinct().count()

    def present_students(self) -> models.QuerySet[StudentProfile]:
        """Students with an accepted check-in for this lecture."""
        return StudentProfile.objects.filter(
            pk__in=self.uploads.accepted().values("student")
        ).select_related("user")

    def absent_students(self) -> models.QuerySet[StudentProfile]:
        """Actively enrolled students with no accepted check-in for this lecture."""
        return (
            StudentProfile.objects.filter(
                enrollments__course_id=self.course_id, enrollments__is_active=True
            )
            .exclude(pk__in=self.uploads.accepted().values("student"))
            .select_related("user")
        )

    def to_dict(self, enrolled: int | None = None, include_students: bool = False) -> dict:
        """
        Return this lecture's attendance as a JSON-serializable dict.

        Args:
            enrolled: Active enrollment count for the course. Pass it when
                exporting many lectures so it's computed only once; if
                omitted, it is queried.
            include_students: Also list each present student's university ID,
                name and check-in time.
        """
        present = self.attendance_count
        if enrolled is None:
            enrolled = self.course.active_enrollments().count()
        data = {
            "id": self.pk,
            "course_id": self.course_id,
            "date": self.date,
            "present": present,
            "enrolled": enrolled,
            "attendance_rate": _rate(present, enrolled),
        }
        if include_students:
            data["students"] = [
                {
                    "university_id": upload.student.university_id,
                    "name": upload.student.display_name,
                    "checked_in_at": upload.uploaded_at,
                }
                for upload in self.uploads.accepted()
                .select_related("student__user")
                .order_by("uploaded_at")
            ]
        return data


# ---------------------------------------------------------------------------
# Uploads (attendance records)
# ---------------------------------------------------------------------------


class QRCodeUploadQuerySet(models.QuerySet):
    """Query helpers for :class:`QRCodeUpload`, available as ``QRCodeUpload.objects.<method>()``."""

    def accepted(self) -> QRCodeUploadQuerySet:
        """Uploads that count as attendance."""
        return self.filter(status=QRCodeUpload.Status.ACCEPTED)

    def rejected(self) -> QRCodeUploadQuerySet:
        """Uploads that were turned down (see ``rejection_reason``)."""
        return self.filter(status=QRCodeUpload.Status.REJECTED)

    def suspicious(self) -> QRCodeUploadQuerySet:
        """
        Rejected uploads that suggest cheating rather than an honest mistake.

        These are photos identical to another student's, and check-ins outside
        the scheduled window. Instructors can review them on the dashboard.
        """
        return self.rejected().filter(
            rejection_reason__in=[
                QRCodeUpload.RejectionReason.DUPLICATE_IMAGE,
                QRCodeUpload.RejectionReason.OUTSIDE_WINDOW,
            ]
        )


class QRCodeUpload(models.Model):
    """
    A photo of a projected QR code that a student uploaded, and the result of checking it.

    An upload with ``status == ACCEPTED`` is the attendance record for
    ``(student, lecture)``. Uploads that fail a check are kept with
    ``status == REJECTED`` and a ``rejection_reason``, which gives
    instructors an audit trail of failed and suspicious attempts.

    Lifecycle::

        submit() ─> PENDING ─process()─┬─> ACCEPTED   (counts as attendance)
                                       └─> REJECTED   (+ rejection_reason)

    :meth:`process` runs these checks in order and stops at the first failure:

    ==================  =========================================================
    Reason              Check
    ==================  =========================================================
    UNREADABLE          The photo contains a decodable QR code.
    UNKNOWN_CODE        The code is an AttendanceChimp code for an existing
                        lecture (forged, rotated and unrelated codes fail).
    NOT_ENROLLED        The student is actively enrolled in that lecture's course.
    OUTSIDE_WINDOW      ``uploaded_at`` (server clock) is inside the lecture's
                        check-in window on a scheduled meeting day.
    DUPLICATE_IMAGE     No *other* student has submitted a byte-identical photo
                        (catches a photo forwarded to friends unmodified).
    ALREADY_RECORDED    The student doesn't already have an accepted check-in
                        for this lecture. This is also enforced by a database
                        constraint, which handles concurrent uploads.
    ==================  =========================================================

    Limitations: ``DUPLICATE_IMAGE`` only matches identical bytes. A photo
    that was re-compressed, cropped or screenshotted gets a different hash.
    The main protection against a code shared from inside the room is the
    short check-in window together with :meth:`Lecture.rotate_qr_token`.

    Attributes:
        student: Who uploaded the photo.
        lecture: The lecture the QR code matched. It is ``None`` when the
            photo couldn't be decoded or the code matched no lecture, which
            is why the link is optional.
        image: The uploaded photo, stored under ``qr_uploads/<student pk>/``.
        image_sha256: SHA-256 of the image bytes, computed on first save.
        decoded_payload: All QR text found in the photo, one per line, kept
            for auditing.
        status: ``pending``, ``accepted`` or ``rejected``.
        rejection_reason: Why the upload was rejected. Empty unless rejected
            (enforced by database constraints).
        uploaded_at: Server time when the upload was received. This is the
            only timestamp used for the time-window check.
        processed_at: When :meth:`process` finished.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    class RejectionReason(models.TextChoices):
        # Labels are written for students: views can show them directly.
        UNREADABLE = (
            "unreadable",
            "No QR code could be read from this photo. Retake it closer to the screen.",
        )
        UNKNOWN_CODE = (
            "unknown_code",
            "This QR code doesn't match any current lecture.",
        )
        NOT_ENROLLED = "not_enrolled", "You aren't enrolled in this course."
        OUTSIDE_WINDOW = (
            "outside_window",
            "Check-in is only open during the scheduled class time.",
        )
        DUPLICATE_IMAGE = (
            "duplicate_image",
            "This exact photo was already submitted by another student.",
        )
        ALREADY_RECORDED = (
            "already_recorded",
            "Your attendance for this lecture is already recorded.",
        )

    student = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="uploads"
    )
    lecture = models.ForeignKey(
        Lecture,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="uploads",
        help_text="Lecture the QR code matched; empty if it matched none.",
    )
    # validate_image_file_extension is listed explicitly because the model-level ImageField
    # (unlike forms.ImageField) doesn't apply it, so submit()'s full_clean() would skip it.
    image = models.ImageField(
        upload_to=qr_upload_path,
        validators=[validate_image_file_extension, validate_upload_size],
    )
    image_sha256 = models.CharField(
        "image SHA-256", max_length=64, blank=True, editable=False, db_index=True
    )
    decoded_payload = models.TextField(blank=True, editable=False)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    rejection_reason = models.CharField(
        max_length=32, choices=RejectionReason.choices, blank=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    objects = QRCodeUploadQuerySet.as_manager()

    #: Fields written by process(). Saving only these avoids overwriting the image.
    _PROCESSING_FIELDS = ["lecture", "decoded_payload", "status", "rejection_reason", "processed_at"]

    class Meta:
        verbose_name = "QR code upload"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["lecture", "status"], name="ac_upload_lecture_status_idx"),
        ]
        constraints = [
            # Core anti-duplication rule: one attendance record per student per lecture.
            # Rejected attempts are unrestricted so they can all be kept for auditing.
            models.UniqueConstraint(
                fields=["student", "lecture"],
                condition=Q(status="accepted"),
                name="ac_one_accepted_upload_per_student_per_lecture",
            ),
            models.CheckConstraint(
                condition=~Q(status="accepted") | Q(lecture__isnull=False, rejection_reason=""),
                name="ac_accepted_upload_has_lecture_and_no_reason",
            ),
            models.CheckConstraint(
                condition=~Q(status="rejected") | ~Q(rejection_reason=""),
                name="ac_rejected_upload_has_reason",
            ),
        ]

    def __str__(self) -> str:
        when = f"{self.uploaded_at:%Y-%m-%d %H:%M}" if self.uploaded_at else "unsaved"
        return f"{self.student} upload at {when} ({self.get_status_display()})"

    def save(self, *args, **kwargs) -> None:
        """Compute ``image_sha256`` the first time the upload is saved with an image."""
        if self.image and not self.image_sha256:
            self.image_sha256 = sha256_of_file(self.image)
        super().save(*args, **kwargs)

    # -- Entry point for views ------------------------------------------------

    @classmethod
    def submit(
        cls,
        student: StudentProfile,
        image: File,
        decoder: QRDecoder | None = None,
    ) -> QRCodeUpload:
        """
        Validate, store and process a student's photo in one call.

        This is all a student-facing view needs::

            upload = QRCodeUpload.submit(request.user.student_profile, request.FILES["image"])
            messages.info(request, upload.user_message)

        Args:
            student: The uploading student (normally the logged-in user's profile).
            image: The uploaded image file.
            decoder: Optional replacement for :func:`decode_qr_payloads`.

        Returns:
            The saved upload, already ACCEPTED or REJECTED.

        Raises:
            ValidationError: if the file is too large or doesn't have an image
                extension. Nothing is stored in that case. A file with an image
                extension but unreadable contents is stored and then rejected
                as UNREADABLE, so the attempt stays in the audit trail.
        """
        upload = cls(student=student, image=image)
        upload.full_clean()
        upload.save()
        return upload.process(decoder=decoder)

    # -- Processing -----------------------------------------------------------

    def process(self, decoder: QRDecoder | None = None) -> QRCodeUpload:
        """
        Decode the photo, match it to a lecture and accept or reject it.

        The checks and their order are listed in the class docstring. The
        method is idempotent: an upload that is no longer PENDING is returned
        unchanged. An unsaved upload is saved first, so ``uploaded_at`` comes
        from the server clock.

        Args:
            decoder: Callable that takes an open image file and returns a
                list of decoded strings. Defaults to :func:`decode_qr_payloads`
                (Pillow + pyzbar).

        Returns:
            ``self``, with ``status`` (and ``rejection_reason`` if rejected) set and saved.
        """
        if self._state.adding:
            self.save()
        if self.status != self.Status.PENDING:
            return self

        decode = decoder or decode_qr_payloads
        with self.image.open("rb") as image_file:
            payloads = decode(image_file)
        self.decoded_payload = "\n".join(payloads)
        if not payloads:
            return self._reject(self.RejectionReason.UNREADABLE)

        tokens = {token for token in map(Lecture.token_from_payload, payloads) if token}
        lecture = Lecture.objects.select_related("course").filter(qr_token__in=tokens).first()
        if lecture is None:
            return self._reject(self.RejectionReason.UNKNOWN_CODE)
        self.lecture = lecture

        if not lecture.course.has_active_student(self.student):
            return self._reject(self.RejectionReason.NOT_ENROLLED)
        if not lecture.is_checkin_open(self.uploaded_at):
            return self._reject(self.RejectionReason.OUTSIDE_WINDOW)
        if self.image_submitted_by_another_student():
            return self._reject(self.RejectionReason.DUPLICATE_IMAGE)
        if lecture.uploads.accepted().filter(student_id=self.student_id).exists():
            return self._reject(self.RejectionReason.ALREADY_RECORDED)
        return self._accept()

    def image_submitted_by_another_student(self) -> bool:
        """Return ``True`` if a different student has uploaded a byte-identical photo."""
        if not self.image_sha256:
            return False
        return (
            QRCodeUpload.objects.filter(image_sha256=self.image_sha256)
            .exclude(student_id=self.student_id)
            .exists()
        )

    @property
    def user_message(self) -> str:
        """A short, student-facing explanation of this upload's outcome."""
        if self.status == self.Status.ACCEPTED:
            return f"You're checked in for {self.lecture}."
        if self.status == self.Status.REJECTED:
            return self.get_rejection_reason_display()
        return "Your photo is being processed."

    def _accept(self) -> QRCodeUpload:
        """
        Mark the upload ACCEPTED and save it.

        If a concurrent upload by the same student for the same lecture was
        accepted first, the conditional unique constraint raises
        :class:`~django.db.IntegrityError`. In that case this upload is
        rejected as ALREADY_RECORDED instead. The savepoint keeps any
        enclosing transaction usable.
        """
        self.status = self.Status.ACCEPTED
        self.rejection_reason = ""
        self.processed_at = timezone.now()
        try:
            with transaction.atomic():
                self.save(update_fields=self._PROCESSING_FIELDS)
        except IntegrityError:
            return self._reject(self.RejectionReason.ALREADY_RECORDED)
        return self

    def _reject(self, reason: str) -> QRCodeUpload:
        """Mark the upload REJECTED with *reason* and save it."""
        self.status = self.Status.REJECTED
        self.rejection_reason = reason
        self.processed_at = timezone.now()
        self.save(update_fields=self._PROCESSING_FIELDS)
        return self
