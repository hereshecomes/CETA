"""Minimal page types for the testbed: a section page holding article pages with one text field.
Nothing here touches permissions, workflows or logging; those are Wagtail's own."""
from django.db import models

from wagtail.admin.panels import FieldPanel
from wagtail.models import Page


class SectionPage(Page):
    subpage_types = ["testsite.ArticlePage"]


class ArticlePage(Page):
    body = models.TextField(blank=True)

    content_panels = Page.content_panels + [FieldPanel("body")]
    parent_page_types = ["testsite.SectionPage"]
