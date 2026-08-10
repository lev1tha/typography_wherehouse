"""Резка разделена на два станка: ЧПУ и лазер.

Заказчик считает резку по станкам, а не по материалам: «сколько наработал ЧПУ и
сколько лазер» — его вопрос, материал в нём вторичен.

Уже заведённые услуги резки НЕ переименовываем: их название стоит в прошлых
чеках, и правка задним числом переписала бы историю. Им только проставляется
станок ЧПУ (единственный, который был), а лазер добавляется рядом.
"""

from django.db import migrations


def split_machines(apps, schema_editor):
    Service = apps.get_model("services", "PrintingService")
    cutting = Service.objects.filter(kind="CUTTING")
    # Всё, что резалось до сих пор, резалось на ЧПУ — другого станка в системе
    # не было. Ставку не трогаем: 0 означает «берём ставку материала», то есть
    # цены остаются ровно теми же, что были до разделения.
    cutting.filter(machine="").update(machine="CNC")

    if not cutting.filter(machine="LASER").exists():
        # Название заполняем во ВСЕХ языковых колонках: у модели включён
        # modeltranslation, и интерфейс читает `name_ru`, а не `name`. Записать
        # одно `name` — получить услугу, которая в интерфейсе называется
        # значением по умолчанию («Резка букв»), как и та, что была.
        Service.objects.create(
            name="Резка лазером",
            name_ru="Резка лазером",
            name_ky="Лазер менен кесүү",
            name_en="Laser cutting",
            kind="CUTTING",
            machine="LASER",
            is_active=True,
        )


def merge_machines(apps, schema_editor):
    Service = apps.get_model("services", "PrintingService")
    Service.objects.filter(kind="CUTTING", machine="LASER", name="Резка лазером").delete()
    Service.objects.filter(kind="CUTTING").update(machine="")


class Migration(migrations.Migration):

    dependencies = [("services", "0002_printingservice_machine_printingservice_rate_per_pm")]

    operations = [migrations.RunPython(split_machines, merge_machines)]
