from django.apps import AppConfig


class WarehouseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'warehouse'

    def ready(self):
        # Снимок склада при закрытии периода (STK-04, волна 2): финансы о складе
        # не знают, склад подписывается на сохранение замка сам.
        from django.db.models.signals import post_save

        from finance.models import PeriodLock

        from .snapshots import on_period_lock_saved

        post_save.connect(on_period_lock_saved, sender=PeriodLock, dispatch_uid="warehouse_snapshot_on_lock")
