# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from .byte import ByteRule
from .bytebonus import ByteBonusRule
from .contrib import ContribRule
from .external_link import ExternalLinkRule
from .image import ImageRule
from .new import NewPageRule
from .qualified import QualiRule
from .redirect import RedirectRule
from .ref import RefRule
from .regexp import RegexpRule, SectionRule
from .templateremoval import TemplateRemovalRule
from .wikidata import WikidataRule
from .word import WordRule
from .wordbonus import WordBonusRule

rule_classes = [
    ByteRule,
    ByteBonusRule,
    ContribRule,
    ExternalLinkRule,
    ImageRule,
    NewPageRule,
    QualiRule,
    RedirectRule,
    RefRule,
    RegexpRule,
    SectionRule,
    TemplateRemovalRule,
    WikidataRule,
    WordRule,
    WordBonusRule,
]
