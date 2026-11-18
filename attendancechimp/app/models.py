"""
Data model for AttendanceChimp, an electronic class attendance tool.

Tables: InstructorProfile, StudentProfile, Course, Enrollment, Lecture,
QRCodeUpload. Enrollment links students to courses (many-to-many). 

Each profile is linked one-to-one to Django's built-in User table (auth_user), which stores the login account.

Each Lecture belongs to a Course, and each QRCodeUpload belongs to a student and,
once its QR code is matched, a lecture.
"""
from zoneinfo import ZoneInfo

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

CHICAGO = ZoneInfo("America/Chicago")

# One letter per weekday, in datetime.weekday() order (Monday = 0 ... Sunday = 6).
# R is Thursday and U is Sunday, so every day has its own letter.
DAY_LETTERS = "MTWRFSU"

class InstructorProfile(models.Model):
    """
    An instructor: one row per instructor. 
    Course instructor can only be chosen from the Instructor Profile.  
 
    Attributes:
        instructor_id (PK): Auto-incrementing integer
        user (FK, unique): One-to-one link to Django's built-in User, which handles login
      (column user_id). Each login account can have at most one
      instructor profile. Deleting the account deletes the profile.
        first_name: The instructor's first name.
        last_name:  The instructor's last name.
    """
    instructor_id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)  # login account; column user_id
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
 
 
class StudentProfile(models.Model):
    """
    A student: one row per student.
    Kept separate from InstructorProfile so that Enrollment and QRCodeUpload can point only to students.
 
    Attributes:
        student_id (PK): Auto-incrementing integer
        user (FK, unique): One-to-one link to Django's built-in User, which handles login
      (column user_id). Each login account can have at most one
      student profile. Deleting the account deletes the profile.
        first_name: The student's first name.
        last_name:  The student's last name.
    """

    student_id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)  # login account; column user_id 
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
 
 
class Course(models.Model):
    """    
    A course and its weekly meeting schedule.
    Each course has exactly one instructor.
    An instructor can teach many courses (one-to-many). 
    The schedule can be used to check if the QR Code is uploaded during the lecture time.
 
    Attributes:
        course_id (PK): Auto-incrementing integer.
        name: The course number, e.g. "DATA13600" (sent as course-name when the course is created). 
                Course numbers are unique, so this identifies the course.
        instructor: The InstructorProfile who teaches the course (column instructor_id). 
                    An instructor who still has courses cannot be deleted.
                    Deleting a person can never silently wipe out courses and attendance history.
        meeting_days: The days the course meets, one letter per day, with R for Thursday (e.g. "TR" = Tuesday and Thursday).
        start_time:   The time class starts.
        end_time:     The time class ends.
    """

    course_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=200)
    instructor = models.ForeignKey(InstructorProfile, on_delete=models.PROTECT)  # column instructor_id
    meeting_days = models.CharField(max_length=7)  
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    def meets_on_day(self, weekday):
        """
        Return True if the course meets on the given weekday.

        Input: weekday as a number, Monday = 0 ... Sunday = 6 (what datetime.weekday() returns).
        It is turned into a letter with DAY_LETTERS (R = Thursday) and looked up in meeting_days.
        Weekends (S, U) are never in meeting_days, so they always return False.
        """
        return DAY_LETTERS[weekday] in self.meeting_days


class Enrollment(models.Model):
    """
    One student enrolled in one course.
    This is the junction table that makes students <-> courses many-to-many.
    Each row links exactly one student to exactly one course,
    while a student can have many enrollments and a course can have many students.

    The pair (student_id, course_id) must be unique, so the database rejects enrolling the same student in the same course twice.

    Attributes:
        enrollment_id (PK): Auto-incrementing integer.
        student: The enrolled StudentProfile (column student_id), deleting a student removes their enrollments.
        course:  The Course (column course_id), deleting a course removes its enrollments.
    """
    enrollment_id = models.AutoField(primary_key=True)  # auto-incrementing integer
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE)  # column student_id
    course = models.ForeignKey(Course, on_delete=models.CASCADE)  # column course_id

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student", "course"], name="unique_enrollment"),
        ]
 
 
class Lecture(models.Model):
    """
    A single class meeting of a course.
    Each lecture has its own QR identifier. 
    When a student uploads a photo of the QR code shown in class, 
    the decoded text is matched against qr_identifier to find which lecture the upload belongs to.
 
    Attributes:
        lecture_id: Auto-incrementing primary key.
        course: The Course this meeting belongs to (column course_id), deleting a course removes its lectures.
        date: The date of the meeting.
        qr_identifier: A random, unique token encoded in this lecture's QR code.
                       Unique (so each token identifies exactly one lecture)
                       Random (so a code from another lecture or a guessed code matches nothing) 
                       Only the text is stored; the QR image can be regenerated from it whenever it is displayed.
    """
    lecture_id = models.AutoField(primary_key=True)  
    course = models.ForeignKey(Course, on_delete=models.CASCADE)  # column course_id
    date = models.DateField()
    qr_identifier = models.CharField(max_length=64, unique=True)
 
 
class QRCodeUpload(models.Model):
    """    
    A photo of a lecture's QR code uploaded by a student.
 
    Every upload is stored, including ones that fail, so the table also works
    as an audit trail for detecting cheating.
 
    Attributes:
        upload_id: Auto-incrementing primary key.
        student: The StudentProfile who uploaded (column student_id), deleting a student removes their uploads.
        lecture: The Lecture the photo was matched to (column lecture_id). Empty until the photo's QR code is decoded and matched.
                 It stays empty if the code matches no lecture (unreadable, old, or forged), so the upload is kept as evidence.
                 SET_NULL: deleting a lecture keeps its uploads but clears the link.
        time_uploaded: Defaults to the server's current time when the upload is created.
                       The upload view never reads a time from the request, so students cannot fake it.
                       An upload counts as attendance only if, in Chicago time, it falls on a course
                       meeting day, between start_time and end_time (see getUploadsForCourse).
        image_uploaded: The uploaded photo (stored as a file path).
    """

    upload_id = models.AutoField(primary_key=True)  # auto-incrementing integer
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE)  # column student_id
    lecture = models.ForeignKey(Lecture, on_delete=models.SET_NULL,
                                null=True, blank=True)  # column lecture_id, may be empty
    time_uploaded = models.DateTimeField(default=timezone.now)  # server time when created
    image_uploaded = models.FileField(upload_to="qr_uploads/")

def getUploadsForCourse(course):
    """
    Return the uploads that count as valid attendance for one course.

    Input:  course, a course number such as "DATA13600" (stored in Course.name).
    Output: a Python list of QRCodeUpload objects, oldest first.
            An empty list if no course has that number.

    Checks:
        1. The course number must refer to an existing course.
        2. Only uploads linked to that course through Upload -> Lecture -> Course are
           considered, so uploads not matched to a lecture are left out.
        3. An upload is valid only if, in Chicago time,
           a. it was made on a day the course meets (meeting_days), and
           b. its time of day is between start_time and end_time (inclusive).
    Timestamps are stored in UTC, so each one is converted to Chicago time before
    the day and time are checked.
    """
    # Newest course with this number: the same rule createLecture uses.
    course_obj = Course.objects.filter(name=course).order_by("-course_id").first()
    if course_obj is None:
        return []

    uploads = (
        QRCodeUpload.objects
        .filter(lecture__course=course_obj)
        .select_related("student__user")
        .order_by("time_uploaded")
    )

    valid = []
    for upload in uploads:
        local = timezone.localtime(upload.time_uploaded, CHICAGO)
        on_meeting_day = course_obj.meets_on_day(local.weekday())
        during_class = course_obj.start_time <= local.time() <= course_obj.end_time
        if on_meeting_day and during_class:
            valid.append(upload)
    return valid