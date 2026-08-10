from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin
from .models import (Event, Candidate, VoteTransaction, Profile, 
                     Category, ActivityLog, Product, ProductCategory, 
                     VotingCode, Ticket, TicketPurchase)
from django.core.mail import send_mail
from django.conf import settings
from django.core.mail import EmailMultiAlternatives


                     
admin.site.register(VotingCode)

# --- Event & Category Admin ---
admin.site.register(Event)
admin.site.register(Category)

# --- Candidate Admin ---
class CandidateAdmin(admin.ModelAdmin):
    list_display = ('name', 'event', 'category') 
    list_filter = ('event', 'category')          
    
admin.site.register(Candidate, CandidateAdmin)
admin.site.register(VoteTransaction)

admin.site.register(Ticket)
admin.site.register(TicketPurchase)

# --- User & Profile Admin ---
class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False

class CustomUserAdmin(UserAdmin):
    inlines = (ProfileInline,)
    list_display = ('username', 'email', 'is_staff', 'is_approved_organizer')
    actions = ['approve_organizers', 'unapprove_organizers']

    def is_approved_organizer(self, obj):
        return hasattr(obj, 'profile') and obj.profile.is_approved_organizer
    is_approved_organizer.boolean = True
    is_approved_organizer.short_description = 'Approved Organizer'

    def approve_organizers(self, request, queryset):
        updated = 0
        for user in queryset:
            if hasattr(user, 'profile'):
                if not user.profile.is_approved_organizer:
                    user.profile.is_approved_organizer = True
                    user.profile.save()
                    updated += 1
                    
                    # Send Approval Email
                    if user.email:
                        subject = "Your FlexyVotes Organizer Account is Approved!"
                        from_email = settings.DEFAULT_FROM_EMAIL
                        
                        text_content = f"Hi {user.username},\n\nGreat news! Your organizer account on FlexyVotes has been approved by the admin.\nYou can now log in and start creating events, managing voting, and selling tickets.\n\nBest regards,\nThe FlexyVotes Team"
                        
                        html_content = f"""
                        <div style="width: 100%; background-color: #F5F5F5; padding: 20px; font-family: Arial, sans-serif;">
                            <div style="max-width: 600px; margin: 0 auto; background-color: #FFFFFF; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.1);">
                                <div style="background-color: #800020; padding: 20px; text-align: center;">
                                    <h1 style="color: #FFD700; margin: 0; font-size: 28px; letter-spacing: 1px;">FlexyVotes</h1>
                                </div>
                                <div style="padding: 30px; color: #1E1E1E; line-height: 1.6;">
                                    <h2 style="color: #800020; margin-top: 0;">Account Approved! 🎉</h2>
                                    <p style="font-size: 16px;">Hi {user.username},</p>
                                    <p style="font-size: 16px;">Great news! Your organizer account on FlexyVotes has been approved by the admin.</p>
                                    <p style="font-size: 16px;">You can now log in and start creating events, managing voting, and selling tickets.</p>
                                    
                                    <a href="http://127.0.0.1:8000/login/" style="display: inline-block; background-color: #FFD700; color: #800020; padding: 12px 30px; text-decoration: none; border-radius: 30px; font-weight: bold; margin-top: 10px;">Log In Now</a>
                                </div>
                                <div style="background-color: #1E1E1E; color: #888; padding: 20px; text-align: center; font-size: 12px;">
                                    &copy; FlexyVotes. All rights reserved.
                                </div>
                            </div>
                        </div>
                        """
                        
                        msg = EmailMultiAlternatives(subject, text_content, from_email, [user.email])
                        msg.attach_alternative(html_content, "text/html")
                        try:
                            msg.send(fail_silently=True)
                        except Exception:
                            pass
        
        self.message_user(request, f"{updated} user(s) successfully approved as organizers and notified via email.")
    approve_organizers.short_description = "Approve selected as Organizers"

    def unapprove_organizers(self, request, queryset):
        updated = 0
        for user in queryset:
            if hasattr(user, 'profile'):
                if user.profile.is_approved_organizer:
                    user.profile.is_approved_organizer = False
                    user.profile.save()
                    updated += 1
        self.message_user(request, f"{updated} user(s) have been unapproved.")
    unapprove_organizers.short_description = "Unapprove selected Organizers"

admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)

# --- Product & ProductCategory Admin ---
admin.site.register(ProductCategory)

class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'price', 'is_active')
    list_filter = ('is_active', 'category')

admin.site.register(Product, ProductAdmin)