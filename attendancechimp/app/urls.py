from django.urls import path
from . import views

urlpatterns = [
    # HW4
    path('', views.index, name='home'),
    path('index.html', views.index, name='index_html'),
    path('new', views.new, name='new'),
    path('createUser', views.createUser, name='createUser'),
    
    # HW2
    path('time', views.time_view, name='time'),
    path('sum', views.sum_view, name='sum'),
]
