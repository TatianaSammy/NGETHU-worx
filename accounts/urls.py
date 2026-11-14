from django.urls import path

from .authViews import (
    CheckUsernameView,
    ResetPasswordRequestView,
    PasswordResetConfirmView,
    ChangePasswordView,
    SessionCheckView,
)

from .LoginView import LoginView
from .LogoutView import LogoutView
from .views_notifications import DashboardNotificationAckView, DashboardNotificationsView
from .ProfileView import (
    ProfileView,
    ProfilePictureUpdateView,
    UserListView,
    UserCreateView,
    UserDetailView,
    UserUpdateView,
    UserDeleteView,
    SectionListView,
    SectionCreateView,
    SectionUpdateView,
    SectionDeleteView,
    GroupListView,
    GroupCreateView,
    GroupUpdateView,
    GroupDeleteView,
    GroupDetailView,
    PermissionListView,
    PermissionCreateView,
    PermissionUpdateView,
    PermissionDeleteView,
    PlantCreateView,
    PlantUpdateView,
    PlantDeleteView,
    LabCreateView,
    LabUpdateView,
    LabDeleteView,
    SessionListView,
    SessionDeleteView,
    SessionTerminateAllView,
    UserSessionListView,
    PasswordHistoryListView,
    AuditLogListView,
    IPRestrictionListView,
    SystemConfigurationView,
    ListImportView,
    SampleDownloadView,
)

app_name = "accounts"

urlpatterns = [

    path("notifications/", DashboardNotificationsView.as_view(), name="notifications"),
    path("notifications/<str:alert_id>/ack/", DashboardNotificationAckView.as_view(), name="notification-ack"),
    # =========================
    # AUTH / SESSION
    # =========================
    path("", ProfileView.as_view(), name="dashboard"),
    path("profile/", ProfileView.as_view(), name="profile"),

    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("session-check/", SessionCheckView.as_view(), name="session_check"),

    path("change-password/", ChangePasswordView.as_view(), name="change_password"),
    path("check-username/", CheckUsernameView.as_view(), name="check_username"),
    path("reset-password-request/", ResetPasswordRequestView.as_view(), name="reset_password_request"),
    path("reset-password/<path:token>/", PasswordResetConfirmView.as_view(), name="password_reset_confirm"),

    # =========================
    # PROFILE
    # =========================
    path(
        "profile/picture/",
        ProfilePictureUpdateView.as_view(),
        name="profile_picture_update",
    ),

    # =========================
    # USER MANAGEMENT
    # =========================
    path("admin/users/", UserListView.as_view(), name="user_list"),
    path("admin/users/create/", UserCreateView.as_view(), name="user_create"),
    path("admin/users/<int:pk>/", UserDetailView.as_view(), name="user_detail"),
    path("admin/users/<int:pk>/edit/", UserUpdateView.as_view(), name="user_update"),
    path("admin/users/<int:pk>/delete/", UserDeleteView.as_view(), name="user_delete"),

    path("admin/sections/", SectionListView.as_view(), name="section_list"),
    path("admin/sections/create/", SectionCreateView.as_view(), name="section_create"),
    path("admin/sections/<int:pk>/edit/", SectionUpdateView.as_view(), name="section_update"),
    path("admin/sections/<int:pk>/delete/", SectionDeleteView.as_view(), name="section_delete"),

    path("admin/groups/", GroupListView.as_view(), name="group_list"),
    path("admin/groups/create/", GroupCreateView.as_view(), name="group_create"),
    path("admin/groups/<int:pk>/", GroupDetailView.as_view(), name="group_detail"),
    path("admin/groups/<int:pk>/edit/", GroupUpdateView.as_view(), name="group_update"),
    path("admin/groups/<int:pk>/delete/", GroupDeleteView.as_view(), name="group_delete"),

    path("admin/permissions/", PermissionListView.as_view(), name="permission_list"),
    path("admin/permissions/create/", PermissionCreateView.as_view(), name="permission_create"),
    path("admin/permissions/<int:pk>/edit/", PermissionUpdateView.as_view(), name="permission_update"),
    path("admin/permissions/<int:pk>/delete/", PermissionDeleteView.as_view(), name="permission_delete"),

    # =========================
    # PLANT MANAGEMENT
    # =========================
    path("admin/plants/create/", PlantCreateView.as_view(), name="plant_create"),
    path("admin/plants/<int:pk>/edit/", PlantUpdateView.as_view(), name="plant_update"),
    path("admin/plants/<int:pk>/delete/", PlantDeleteView.as_view(), name="plant_delete"),

    # =========================
    # LAB MANAGEMENT
    # =========================
    path("admin/labs/create/", LabCreateView.as_view(), name="lab_create"),
    path("admin/labs/<int:pk>/edit/", LabUpdateView.as_view(), name="lab_update"),
    path("admin/labs/<int:pk>/delete/", LabDeleteView.as_view(), name="lab_delete"),

    # =========================
    # SYSTEM MONITORING
    # =========================
    path("admin/sessions/", SessionListView.as_view(), name="session_list"),
    path("admin/sessions/<str:session_key>/delete/", SessionDeleteView.as_view(), name="session_delete"),
    path("admin/sessions/terminate-all/", SessionTerminateAllView.as_view(), name="session_terminate_all"),
    path("admin/user-sessions/", UserSessionListView.as_view(), name="user_session_list"),
    path("admin/password-history/", PasswordHistoryListView.as_view(), name="password_history_list"),
    path("admin/audit-logs/", AuditLogListView.as_view(), name="audit_log_list"),
    path("admin/ip-restrictions/", IPRestrictionListView.as_view(), name="ip_restriction_list"),
    path("admin/configuration/", SystemConfigurationView.as_view(), name="configuration"),

    # =========================
    # IMPORT & EXPORT TEMPLATES
    # =========================
    path("admin/import/<str:resource>/", ListImportView.as_view(), name="list_import"),
    path("admin/export-sample/<str:resource>/", SampleDownloadView.as_view(), name="sample_download"),
]
