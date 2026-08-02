"""Постраничный вывод с настраиваемым размером страницы."""
from rest_framework.pagination import PageNumberPagination


class SizedPageNumberPagination(PageNumberPagination):
    """Стандартная постраничная выдача, но с ?page_size=.

    Нужна там, где список идёт не в таблицу, а в выпадающий список: каталог
    материалов должен приехать целиком, иначе выбрать можно только первые 25.
    Потолок оставляем, чтобы ?page_size=100000 не выгружал базу целиком.
    """

    page_size_query_param = "page_size"
    max_page_size = 500
