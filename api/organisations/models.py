from __future__ import unicode_literals

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from common.core.utils import is_enterprise, is_saas
from django.conf import settings
from django.core.cache import caches
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django_lifecycle import (  # type: ignore[import-untyped]
    AFTER_CREATE,
    AFTER_SAVE,
    BEFORE_DELETE,
    LifecycleModelMixin,
    hook,
)
from flag_engine.identities.traits.types import TraitValue
from simple_history.models import HistoricalRecords  # type: ignore[import-untyped]

from core.models import SoftDeleteExportableModel
from features.versioning.constants import DEFAULT_VERSION_LIMIT_DAYS
from integrations.lead_tracking.hubspot.tasks import (
    track_hubspot_lead_v2,
    update_hubspot_active_subscription,
)
from organisations.chargebee import (  # type: ignore[attr-defined]
    get_customer_id_from_subscription_id,
    get_max_api_calls_for_plan,
    get_max_seats_for_plan,
    get_plan_meta_data,
    get_portal_url,
    get_subscription_metadata_from_id,
)
from organisations.chargebee.chargebee import add_single_seat
from organisations.chargebee.chargebee import (
    cancel_subscription as cancel_chargebee_subscription,
)
from organisations.chargebee.metadata import ChargebeeObjMetadata
from organisations.subscriptions.constants import (
    CHARGEBEE,
    FREE_PLAN_ID,
    FREE_PLAN_SUBSCRIPTION_METADATA,
    MAX_API_CALLS_IN_FREE_PLAN,
    MAX_SEATS_IN_FREE_PLAN,
    SUBSCRIPTION_BILLING_STATUSES,
    SUBSCRIPTION_PAYMENT_METHODS,
    TRIAL_SUBSCRIPTION_ID,
    XERO,
    SubscriptionPlanFamily,
)
from organisations.subscriptions.exceptions import (
    SubscriptionDoesNotSupportSeatUpgrade,
)
from organisations.subscriptions.metadata import BaseSubscriptionMetadata
from organisations.subscriptions.xero.metadata import XeroSubscriptionMetadata
from webhooks.models import AbstractBaseExportableWebhookModel

environment_cache = caches[settings.ENVIRONMENT_CACHE_NAME]


class OrganisationRole(models.TextChoices):
    ADMIN = ("ADMIN", "Admin")
    USER = ("USER", "User")


class Organisation(LifecycleModelMixin, SoftDeleteExportableModel):  # type: ignore[misc]
    name = models.CharField(max_length=2000)
    has_requested_features = models.BooleanField(default=False)
    webhook_notification_email = models.EmailField(null=True, blank=True)
    created_date = models.DateTimeField("DateCreated", auto_now_add=True)
    alerted_over_plan_limit = models.BooleanField(default=False)
    stop_serving_flags = models.BooleanField(
        default=False,
        help_text="Enable this to cease serving flags for this organisation.",
    )
    restrict_project_create_to_admin = models.BooleanField(default=False)
    persist_trait_data = models.BooleanField(
        default=settings.DEFAULT_ORG_STORE_TRAITS_VALUE,
        help_text="Disable this if you don't want Flagsmith "
        "to store trait data for this org's identities.",
    )
    block_access_to_admin = models.BooleanField(
        default=False,
        help_text="Enable this to block all the access to admin "
        "interface for the organisation",
    )
    feature_analytics = models.BooleanField(
        default=False, help_text="Record feature analytics in InfluxDB"
    )
    force_2fa = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]

    def __str__(self):  # type: ignore[no-untyped-def]
        return "Org %s (#%s)" % (self.name, self.id)

    # noinspection PyTypeChecker
    def get_unique_slug(self):  # type: ignore[no-untyped-def]
        return str(self.id) + "-" + self.name

    @property
    def num_seats(self):  # type: ignore[no-untyped-def]
        return self.users.count()

    def has_paid_subscription(self) -> bool:
        # Includes subscriptions that are canceled.
        # See is_paid for active paid subscriptions only.
        return hasattr(self, "subscription") and bool(self.subscription.subscription_id)

    def has_subscription_information_cache(self) -> bool:
        return hasattr(self, "subscription_information_cache") and bool(
            self.subscription_information_cache
        )

    @property
    def is_paid(self):  # type: ignore[no-untyped-def]
        return (
            self.has_paid_subscription() and self.subscription.cancellation_date is None
        )

    def has_enterprise_subscription(self) -> bool:
        return self.is_paid and self.subscription.is_enterprise

    @property
    def flagsmith_identifier(self):  # type: ignore[no-untyped-def]
        return f"org.{self.id}"

    @property
    def flagsmith_on_flagsmith_api_traits(self) -> dict[str, TraitValue]:
        return {
            "organisation.id": self.id,
            "organisation.name": self.name,
            "subscription.plan": self.subscription.plan,
        }

    def over_plan_seats_limit(self, additional_seats: int = 0):  # type: ignore[no-untyped-def]
        if self.has_paid_subscription():
            susbcription_metadata = self.subscription.get_subscription_metadata()
            return self.num_seats + additional_seats > susbcription_metadata.seats

        return self.num_seats + additional_seats > getattr(
            self.subscription, "max_seats", MAX_SEATS_IN_FREE_PLAN
        )

    def reset_alert_status(self):  # type: ignore[no-untyped-def]
        self.alerted_over_plan_limit = False
        self.save()

    def is_auto_seat_upgrade_available(self) -> bool:
        return self.subscription.can_auto_upgrade_seats

    @hook(BEFORE_DELETE)
    def cancel_subscription(self):  # type: ignore[no-untyped-def]
        if self.has_paid_subscription():
            self.subscription.prepare_for_cancel()

    @hook(AFTER_CREATE)
    def create_subscription(self):  # type: ignore[no-untyped-def]
        Subscription.objects.create(organisation=self)

    @hook(AFTER_CREATE)
    def create_subscription_cache(self):  # type: ignore[no-untyped-def]
        if is_saas() and not self.has_subscription_information_cache():
            OrganisationSubscriptionInformationCache.objects.create(organisation=self)

    @hook(AFTER_SAVE)
    def clear_environment_caches(self):  # type: ignore[no-untyped-def]
        from environments.models import Environment

        environment_cache.delete_many(
            list(
                Environment.objects.filter(project__organisation=self).values_list(
                    "api_key", flat=True
                )
            )
        )

    @hook(AFTER_SAVE, when="stop_serving_flags", has_changed=True)
    def rebuild_environments(self):  # type: ignore[no-untyped-def]
        # Avoid circular imports.
        from environments.models import Environment
        from environments.tasks import rebuild_environment_document

        for environment_id in Environment.objects.filter(
            project__organisation=self
        ).values_list("id", flat=True):
            rebuild_environment_document.delay(args=(environment_id,))

    def cancel_users(self):  # type: ignore[no-untyped-def]
        remaining_seat_holder = (
            UserOrganisation.objects.filter(
                organisation=self,
                role=OrganisationRole.ADMIN,
            )
            .order_by("date_joined")
            .first()
        )

        UserOrganisation.objects.filter(
            organisation=self,
        ).exclude(
            id=remaining_seat_holder.id  # type: ignore[union-attr]
        ).delete()


class UserOrganisation(LifecycleModelMixin, models.Model):  # type: ignore[misc]
    user = models.ForeignKey("users.FFAdminUser", on_delete=models.CASCADE)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE)
    date_joined = models.DateTimeField(auto_now_add=True)
    role = models.CharField(max_length=50, choices=OrganisationRole.choices)

    class Meta:
        unique_together = (
            "user",
            "organisation",
        )

    @hook(AFTER_CREATE)
    def register_hubspot_lead_tracking(self):  # type: ignore[no-untyped-def]
        if settings.ENABLE_HUBSPOT_LEAD_TRACKING:
            track_hubspot_lead_v2.delay(
                args=(
                    self.user.id,
                    self.organisation.id,
                ),
                delay_until=timezone.now() + timedelta(minutes=3),
            )


class Subscription(LifecycleModelMixin, SoftDeleteExportableModel):  # type: ignore[misc]
    # Even though it is not enforced at the database level,
    # every organisation has a subscription.
    organisation = models.OneToOneField(
        Organisation, on_delete=models.CASCADE, related_name="subscription"
    )
    subscription_id = models.CharField(max_length=100, blank=True, null=True)
    subscription_date = models.DateTimeField(blank=True, null=True)
    plan = models.CharField(max_length=100, null=True, blank=True, default=FREE_PLAN_ID)
    max_seats = models.IntegerField(default=MAX_SEATS_IN_FREE_PLAN)
    max_api_calls = models.BigIntegerField(default=MAX_API_CALLS_IN_FREE_PLAN)
    cancellation_date = models.DateTimeField(blank=True, null=True)
    customer_id = models.CharField(max_length=100, blank=True, null=True)

    # Free and cancelled subscriptions are blank.
    billing_status = models.CharField(
        max_length=20,
        choices=SUBSCRIPTION_BILLING_STATUSES,
        blank=True,
        null=True,
    )
    payment_method = models.CharField(
        max_length=20,
        choices=SUBSCRIPTION_PAYMENT_METHODS,
        blank=True,
        null=True,
    )
    notes = models.CharField(max_length=500, blank=True, null=True)

    # Intentionally avoid the AuditLog for subscriptions.
    history = HistoricalRecords()

    def update_plan(self, plan_id):  # type: ignore[no-untyped-def]
        plan_metadata = get_plan_meta_data(plan_id)  # type: ignore[no-untyped-call]
        self.cancellation_date = None
        self.plan = plan_id
        self.max_seats = get_max_seats_for_plan(plan_metadata)
        self.max_api_calls = get_max_api_calls_for_plan(plan_metadata)
        self.save()

    @property
    def can_auto_upgrade_seats(self) -> bool:
        return (
            is_saas()
            and self.subscription_plan_family == SubscriptionPlanFamily.SCALE_UP
        )

    @property
    def has_active_billing_periods(self) -> bool:
        return (
            self.organisation.has_subscription_information_cache()
            and self.organisation.subscription_information_cache.has_active_billing_periods()
        )

    @property
    def is_free_plan(self) -> bool:
        return self.subscription_plan_family == SubscriptionPlanFamily.FREE

    @property
    def subscription_plan_family(self) -> SubscriptionPlanFamily:
        return SubscriptionPlanFamily.get_by_plan_id(self.plan)  # type: ignore[arg-type]

    @property
    def is_enterprise(self) -> bool:
        return self.subscription_plan_family == SubscriptionPlanFamily.ENTERPRISE

    @hook(AFTER_SAVE, when="plan", has_changed=True)
    def update_api_limit_access_block(self):  # type: ignore[no-untyped-def]
        if not getattr(self.organisation, "api_limit_access_block", None):
            return

        self.organisation.api_limit_access_block.delete()
        self.organisation.stop_serving_flags = False
        self.organisation.block_access_to_admin = False
        self.organisation.save()

    @hook(AFTER_SAVE, when="plan", has_changed=True)
    def update_hubspot_active_subscription(self):  # type: ignore[no-untyped-def]
        if not settings.ENABLE_HUBSPOT_LEAD_TRACKING:
            return

        update_hubspot_active_subscription.delay(args=(self.id,))

    def save_as_free_subscription(self):  # type: ignore[no-untyped-def]
        """
        Wipes a subscription to a normal free plan.

        The only normal field that is retained is the notes field.
        """
        self.subscription_id = None
        self.subscription_date = None
        self.plan = FREE_PLAN_ID
        self.max_seats = MAX_SEATS_IN_FREE_PLAN
        self.max_api_calls = MAX_API_CALLS_IN_FREE_PLAN
        self.cancellation_date = None
        self.customer_id = None
        self.billing_status = None
        self.payment_method = None

        self.save()

        if not getattr(self.organisation, "subscription_information_cache", None):
            return

        self.organisation.subscription_information_cache.reset_to_defaults()  # type: ignore[no-untyped-call]
        self.organisation.subscription_information_cache.save()

    def prepare_for_cancel(  # type: ignore[no-untyped-def]
        self, cancellation_date=timezone.now(), update_chargebee=True
    ) -> None:
        """
        This method gets a subscription ready for cancellation.

        If cancellation_date is in the future some aspects are
        reserved for a task after the date has passed.
        """
        # Avoid circular import.
        from organisations.tasks import send_org_subscription_cancelled_alert

        if self.payment_method == CHARGEBEE and update_chargebee:
            cancel_chargebee_subscription(self.subscription_id)  # type: ignore[arg-type]

        send_org_subscription_cancelled_alert.delay(
            kwargs={
                "organisation_name": self.organisation.name,
                "formatted_cancellation_date": cancellation_date.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )

        if cancellation_date <= timezone.now():
            # Since the date is immediate, wipe data right away.
            self.organisation.cancel_users()  # type: ignore[no-untyped-call]
            self.save_as_free_subscription()  # type: ignore[no-untyped-call]
            return

        # Since the date is in the future, a task takes it.
        self.cancellation_date = cancellation_date
        self.billing_status = None
        self.save()

    def get_portal_url(self, redirect_url):  # type: ignore[no-untyped-def]
        if not self.subscription_id:
            return None

        if not self.customer_id:
            self.customer_id = get_customer_id_from_subscription_id(  # type: ignore[no-untyped-call]
                self.subscription_id
            )
            self.save()
        return get_portal_url(self.customer_id, redirect_url)  # type: ignore[no-untyped-call]

    def get_subscription_metadata(self) -> BaseSubscriptionMetadata:
        if self.is_free_plan:
            # Free plan is the default everywhere, we should prevent
            # increased access for all deployment types on the free
            # plan.
            return FREE_PLAN_SUBSCRIPTION_METADATA

        return (
            self._get_subscription_metadata_for_saas()
            if is_saas()
            else self._get_subscription_metadata_for_self_hosted()
        )

    def _get_subscription_metadata_for_saas(self) -> BaseSubscriptionMetadata:
        if self.payment_method == CHARGEBEE and self.subscription_id:
            return self._get_subscription_metadata_for_chargebee()
        elif self.payment_method == XERO and self.subscription_id:
            return XeroSubscriptionMetadata(
                seats=self.max_seats, api_calls=self.max_api_calls
            )

        # Default fall through here means this is a manually added subscription
        # or for a payment method that is not covered above. In this situation
        # we want the response to be what is stored in the Django database.
        # Note that Free plans are caught in the parent method above.
        if self.organisation.has_subscription_information_cache():
            return self.organisation.subscription_information_cache.as_base_subscription_metadata(
                seats=self.max_seats,
                api_calls=self.max_api_calls,
            )
        return BaseSubscriptionMetadata(
            seats=self.max_seats, api_calls=self.max_api_calls
        )

    def _get_subscription_metadata_for_chargebee(self) -> ChargebeeObjMetadata:
        if self.organisation.has_subscription_information_cache():
            # Getting the data from the subscription information cache because
            # data is guaranteed to be up to date by using a Chargebee webhook.
            cb_metadata = self.organisation.subscription_information_cache.as_chargebee_subscription_metadata()
        else:
            cb_metadata = get_subscription_metadata_from_id(self.subscription_id)  # type: ignore[assignment,arg-type]

        if self.subscription_plan_family == SubscriptionPlanFamily.SCALE_UP and (
            settings.VERSIONING_RELEASE_DATE is None
            or (
                self.subscription_date is not None
                and self.subscription_date < settings.VERSIONING_RELEASE_DATE
            )
        ):
            # Logic to grandfather old scale up plan customers to give them
            # full access to audit log and feature history.
            cb_metadata.audit_log_visibility_days = None
            cb_metadata.feature_history_visibility_days = None

        return cb_metadata

    def _get_subscription_metadata_for_self_hosted(self) -> BaseSubscriptionMetadata:
        if is_enterprise() and hasattr(
            self.organisation, "licence"
        ):  # pragma: no cover
            licence_information = self.organisation.licence.get_licence_information()
            return BaseSubscriptionMetadata(
                seats=licence_information.num_seats,
                projects=licence_information.num_projects,
                audit_log_visibility_days=None,
                feature_history_visibility_days=None,
            )
        # TODO: Once we've successfully rolled out licences to enterprises
        #       remove this branch to force them into the free plan
        #       if they don't have a licence.
        elif is_enterprise():  # pragma: no cover
            return BaseSubscriptionMetadata(
                seats=self.max_seats,
                api_calls=self.max_api_calls,
                projects=None,
                audit_log_visibility_days=None,
                feature_history_visibility_days=None,
            )

        return FREE_PLAN_SUBSCRIPTION_METADATA

    def add_single_seat(self):  # type: ignore[no-untyped-def]
        if not self.can_auto_upgrade_seats:
            raise SubscriptionDoesNotSupportSeatUpgrade()

        add_single_seat(self.subscription_id)  # type: ignore[arg-type]

    def is_in_trial(self) -> bool:
        return self.subscription_id == TRIAL_SUBSCRIPTION_ID


class OrganisationWebhook(AbstractBaseExportableWebhookModel):
    name = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="webhooks"
    )
    created_at = models.DateTimeField(null=True, auto_now_add=True)
    updated_at = models.DateTimeField(null=True, auto_now=True)

    class Meta:
        ordering = ("id",)  # explicit ordering to prevent pagination warnings


class OrganisationSubscriptionInformationCache(LifecycleModelMixin, models.Model):  # type: ignore[misc]
    """
    Model to hold a cache of an organisation's API usage and their Chargebee plan limits.
    """

    organisation = models.OneToOneField(
        Organisation,
        related_name="subscription_information_cache",
        on_delete=models.CASCADE,
    )
    updated_at = models.DateTimeField(auto_now=True)
    chargebee_updated_at = models.DateTimeField(auto_now=False, null=True)
    influx_updated_at = models.DateTimeField(auto_now=False, null=True)
    current_billing_term_starts_at = models.DateTimeField(auto_now=False, null=True)
    current_billing_term_ends_at = models.DateTimeField(auto_now=False, null=True)

    api_calls_24h = models.IntegerField(default=0)
    api_calls_7d = models.IntegerField(default=0)
    api_calls_30d = models.IntegerField(default=0)

    allowed_seats = models.IntegerField(default=MAX_SEATS_IN_FREE_PLAN)
    allowed_30d_api_calls = models.IntegerField(default=MAX_API_CALLS_IN_FREE_PLAN)
    allowed_projects = models.IntegerField(default=1, blank=True, null=True)

    audit_log_visibility_days = models.IntegerField(default=0, null=True, blank=True)
    feature_history_visibility_days = models.IntegerField(
        default=DEFAULT_VERSION_LIMIT_DAYS, null=True, blank=True
    )

    chargebee_email = models.EmailField(blank=True, max_length=254, null=True)

    @hook(AFTER_SAVE, when="allowed_30d_api_calls", has_changed=True)
    def erase_api_notifications(self):  # type: ignore[no-untyped-def]
        self.organisation.api_usage_notifications.all().delete()

    def upgrade_to_enterprise(self, seats: int, api_calls: int):  # type: ignore[no-untyped-def]
        self.allowed_seats = seats
        self.allowed_30d_api_calls = api_calls

        self.allowed_projects = None
        self.audit_log_visibility_days = None
        self.feature_history_visibility_days = None

    def reset_to_defaults(self):  # type: ignore[no-untyped-def]
        """
        Resets all limits and CB related data to the defaults, leaving the
        usage data intact.
        """
        self.current_billing_term_starts_at = None
        self.current_billing_term_ends_at = None

        self.allowed_seats = MAX_SEATS_IN_FREE_PLAN
        self.allowed_30d_api_calls = MAX_API_CALLS_IN_FREE_PLAN
        self.allowed_projects = 1
        self.audit_log_visibility_days = 0
        self.feature_history_visibility_days = DEFAULT_VERSION_LIMIT_DAYS

        self.chargebee_email = None

    def as_base_subscription_metadata(self, **overrides) -> BaseSubscriptionMetadata:  # type: ignore[no-untyped-def]
        kwargs = {
            **self._get_default_subscription_metadata_kwargs(),
            **overrides,
        }
        return BaseSubscriptionMetadata(**kwargs)

    def as_chargebee_subscription_metadata(self, **overrides) -> ChargebeeObjMetadata:  # type: ignore[no-untyped-def]
        kwargs = {
            **self._get_default_subscription_metadata_kwargs(),
            "chargebee_email": self.chargebee_email,
            **overrides,
        }
        return ChargebeeObjMetadata(**kwargs)

    def _get_default_subscription_metadata_kwargs(self) -> dict[str, Any]:
        return {
            "seats": self.allowed_seats,
            "api_calls": self.allowed_30d_api_calls,
            "projects": self.allowed_projects,
            "audit_log_visibility_days": self.audit_log_visibility_days,
            "feature_history_visibility_days": self.feature_history_visibility_days,
        }

    def is_billing_terms_dates_set(self) -> bool:
        return (
            self.current_billing_term_starts_at is not None
            and self.current_billing_term_ends_at is not None
        )

    def has_active_billing_periods(self) -> bool:
        """
        Returns True if current date is within the billing term.
        If either start or end date is None, returns False.
        """
        if not self.is_billing_terms_dates_set():
            return False

        if TYPE_CHECKING:
            assert self.current_billing_term_starts_at is not None
            assert self.current_billing_term_ends_at is not None

        starts_at = self.current_billing_term_starts_at
        ends_at = self.current_billing_term_ends_at
        now = timezone.now()

        return starts_at <= now <= ends_at


class OrganisationAPIUsageNotification(models.Model):
    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="api_usage_notifications"
    )
    percent_usage = models.IntegerField(
        null=False,
        validators=[MinValueValidator(75), MaxValueValidator(500)],
    )
    notified_at = models.DateTimeField(null=True)

    created_at = models.DateTimeField(null=True, auto_now_add=True)
    updated_at = models.DateTimeField(null=True, auto_now=True)


class OrganisationBreachedGracePeriod(models.Model):
    organisation = models.OneToOneField(
        Organisation, on_delete=models.CASCADE, related_name="breached_grace_period"
    )
    created_at = models.DateTimeField(null=True, auto_now_add=True)
    updated_at = models.DateTimeField(null=True, auto_now=True)


class APILimitAccessBlock(models.Model):
    organisation = models.OneToOneField(
        Organisation, on_delete=models.CASCADE, related_name="api_limit_access_block"
    )

    created_at = models.DateTimeField(null=True, auto_now_add=True)
    updated_at = models.DateTimeField(null=True, auto_now=True)


class HubspotOrganisation(models.Model):
    organisation = models.OneToOneField(
        Organisation,
        related_name="hubspot_organisation",
        on_delete=models.CASCADE,
    )
    hubspot_id = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class OrganisationAPIBilling(models.Model):
    """
    Tracks API billing for when accounts go over their API usage
    limits. This model is what allows subsequent billing runs
    to not double bill an organisation for the same use.

    Even though api_overage is charge per 100k API calls, this
    class tracks the actual rounded count of API calls that are
    billed for (i.e., 200000 for an account with 234323 api calls
    and a allowed_30d_api_calls set to 100000, the overage is
    beyond the allowed api calls).
    We're intentionally rounding up to the closest hundred thousand.

    The option to set immediate_invoice means whether or not the
    API billing was processed immediately versus pushed onto the
    subsequent subscription billing period.
    """

    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="api_billing"
    )
    api_overage = models.IntegerField(null=False)
    immediate_invoice = models.BooleanField(null=False, default=False)
    billed_at = models.DateTimeField(null=False)

    created_at = models.DateTimeField(null=True, auto_now_add=True)
    updated_at = models.DateTimeField(null=True, auto_now=True)
