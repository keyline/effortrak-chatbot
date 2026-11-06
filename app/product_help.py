"""Reviewed product information, separate from employee-report context."""

import re

from app.task_report import AssistantReply


PRODUCT_OVERVIEW = (
    'Effortrak is a workforce management application that helps organizations '
    'manage employee attendance, leave, tasks, projects, booked effort and client visits.\n\n'
    'Employees can use the web portal and mobile application to access their '
    'authorized information and supported workflows. Managers and administrators '
    'can review permitted employee and team information to support day-to-day operations.\n\n'
    'The AI Assistant provides guidance on using Effortrak and helps retrieve '
    'supported reports and exports, subject to access permissions.\n\n'
    'Available modules and features depend on the company subscription, '
    'configuration and user permissions.'
)

PRODUCT_QUESTION = re.compile(
    r'\s*(?:(?:hi|hello)[,!]?\s+)?(?:please\s+)?'
    r'(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?'
    r'(?:what\s+is\s+effortrak(?:\s+(?:app|application|platform|software))?'
    r'|(?:tell\s+me|explain|describe|introduce)(?:\s+(?:briefly|more))?\s+(?:about\s+)?effortrak'
    r'|(?:give|show)\s+me\s+(?:a|an|the)\s+(?:brief\s+)?(?:overview|introduction)\s+(?:of|to)\s+effortrak'
    r'|what\s+does\s+effortrak\s+do'
    r'|what\s+(?:is\s+effortrak\s+used\s+for|are\s+(?:the\s+)?(?:main\s+)?(?:features|modules)\s+of\s+effortrak))'
    r'(?:\s+please)?[?.!]*\s*', re.I)

PRODUCT_FOLLOWUP = re.compile(
    r'\s*(?:(?:and\s+)?(?:what\s+about\s+)?(?:its\s+)?(?:main\s+)?(?:features|modules)'
    r'|tell\s+me\s+more(?:\s+about\s+(?:it|effortrak))?)[?.!]*\s*', re.I)


def product_reply():
    return (AssistantReply(PRODUCT_OVERVIEW,
        model_content='A reviewed Effortrak product introduction was displayed; no company records were read.',
        query_context={'kind': 'product_help'}), [], [])


def product_help(message, context):
    if PRODUCT_QUESTION.fullmatch(message) or (
            context.get('kind') == 'product_help' and PRODUCT_FOLLOWUP.fullmatch(message)):
        return product_reply()
    return None
