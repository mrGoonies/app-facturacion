from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

app_name = "tracker"

urlpatterns = [
    path("login/", views.BrandedLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    path("solicitudes/nueva/", views.purchase_request_create, name="purchase_request_create"),
    path("solicitudes/<uuid:token>/", views.request_status, name="request_status"),
    path("logistica/entrega/", views.logistics_handoff_create, name="logistics_handoff"),

    path("panel/", views.queue, name="queue"),
    path("panel/compras/<int:pk>/", views.purchase_detail, name="purchase_detail"),
    path("panel/picking/<str:number>/", views.picking_list_detail, name="picking_list_detail"),
    path("panel/kpis/", views.kpi_scorecard, name="kpi_scorecard"),
]
