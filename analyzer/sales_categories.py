"""Explicit category paths; parallel category entries never imply a hierarchy."""
import re


def category_path(value):
    return [part.strip() for part in re.split(r'\s*(?:/|／|>|＞|›|→)\s*',value) if part.strip()]


def category_matches(values,selected):
    target=category_path(selected)
    return any(category_path(value)[:len(target)]==target for value in values) if target else False
