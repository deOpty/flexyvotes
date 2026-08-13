from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from voting import views

urlpatterns = [
    path('ussd/callback/', views.ussd_callback, name='ussd_callback'),
    path('admin/', admin.site.urls),
    path('', include('voting.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
