from modeltranslation.translator import TranslationOptions, register

from .models import PrintingService


@register(PrintingService)
class PrintingServiceTranslationOptions(TranslationOptions):
    fields = ("name",)
