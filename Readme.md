# Futurestack

Futurestack is a brand-governance framework that helps teams check marketing
content against their own product knowledge and brand rules.

## What it does

When someone submits an image for review, Futurestack reads its text and checks
it against the team's configured knowledge base. It returns potential issues,
explains why they may need attention, and suggests next steps. Reviews are
advisory; a person makes the final decision.

The framework is intended to work where teams already collaborate: people
submit content through a command in a tool such as Discord or Microsoft Teams,
and receive feedback there. Product knowledge stays with the team's working
environment rather than relying on a shared, universal product catalog.

## This prototype

This project is a judging demo of that idea. It provides a web page for image
uploads and a Discord `/check` command, both using the example knowledge base
configured for this project. It does not connect to a live product catalog, and
Microsoft Teams support is not included in the prototype.
