"""News sources module."""

from src.news.sources.google_news import GoogleNewsSource
from src.news.sources.scraper import ArticleScraper

__all__ = ["ArticleScraper", "GoogleNewsSource"]
