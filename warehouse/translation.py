from modeltranslation.translator import TranslationOptions, register

from .models import Material


@register(Material)
class MaterialTranslationOptions(TranslationOptions):
    # Dynamic catalogue text is multilingual: RU / KY / EN.
    fields = ("name", "category")
