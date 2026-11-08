from django.urls import path
from . import views

urlpatterns = [
    # HW4
    path('', views.index, name='home'),
    path('index.html', views.index, name='index_html'),
    path('new', views.new, name='new'),
    path('createUser', views.createUser, name='createUser'),
    
    # HW5: form pages
    path('new_course', views.new_course, name='new_course'),
    path('new_lecture', views.new_lecture, name='new_lecture'),
    path('new_qr_upload', views.new_qr_upload, name='new_qr_upload'),
    
    # HW5: API endpoints
    path('createCourse', views.createCourse, name='createCourse'),
    path('createLecture', views.createLecture, name='createLecture'),
    path('createQRCodeUpload', views.createQRCodeUpload, name='createQRCodeUpload'),
    path('dumpUploads', views.dumpUploads, name='dumpUploads'),

    # HW2
    path('time', views.time_view, name='time'),
    path('sum', views.sum_view, name='sum'),
]
