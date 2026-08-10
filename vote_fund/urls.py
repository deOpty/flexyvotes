from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from voting import views # Add this import

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('voting.urls')),
    path('login/', views.login_view, name='login'), # Custom login view
    path('logout/', views.logout_view, name='logout'), # Custom logout view
    path('register/', views.register_view, name='register'), # Custom register view
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from voting import views # Import views

urlpatterns = [
    path('ussd/callback/', views.ussd_callback, name='ussd_callback'), # Add this line
    path('admin/', admin.site.urls),
    path('', include('voting.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)