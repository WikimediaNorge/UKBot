# encoding=utf-8
# vim: fenc=utf-8 noet sw=4 ts=4 sts=4 ai

# Add more languages here!  Beware that this is a makefile snippet and
# you have to adhere to make syntax.
LINGUAS = nb_NO \
	ca_ES \
	fi_FI \
	eu_ES

# Textdomain for our package.
TEXTDOMAIN = messages

localedir = ./locale
targetdir = ./locale
sourcedir = ./ukbot

CATALOGS = $(LINGUAS)
MO_FILES = $(addprefix $(localedir)/, $(addsuffix .mo, $(LINGUAS)))
SOURCE_FILES = $(wildcard ukbot/*.py ukbot/rules/*.py ukbot/filters/*.py)
MSGMERGE = msgmerge
MSGFMT   = msgfmt
XGETTEXT = xgettext
CATOBJEXT = .po

TD = $(strip $(TEXTDOMAIN))

default: help

all: $(TD).pot update-po update-mo install
# makedb

help:
	@echo "Available targets:"
	@echo "  pot          - Regenerate master catalog from source files"
	@echo "  update-po    - Update po files from master catalog"
	@echo "  update-mo    - Regenerate mo files from po files"
	@echo "  install      - Install mo files"
	@echo "  all          - All of the above"

#POTFILES = $(localedir)/POTFILES.in \
#	$(shell cat $(localedir)/POTFILES.in)

pot: $(TD).pot

clean:
	for dir in ./ $(localedir); do \
		rm -fv $$dir/*~ $$dir/*.bak $$dir/*.mo $$dir/*.pox $$dir/*.pot; \
	done


# Use xgettext to extract strings from the source code
# files listed in POTFILESS.in

$(TD).pot: $(SOURCE_FILES)
	$(XGETTEXT) --output=$(localedir)/$(TD).pox $(SOURCE_FILES)
	rm -f $(localedir)/$@ && mv $(localedir)/$(TD).pox $(localedir)/$@

install: $(MO_FILES)
	languages='$(LINGUAS)'; \
	for lang in $$languages; do \
		mkdir -p "$(targetdir)/$$lang/LC_MESSAGES" || exit 1; \
		dest="$(targetdir)/$$lang/LC_MESSAGES/$(TD).mo"; \
		cat="$(localedir)/$$lang.mo"; \
		echo "installing $$cat as $$dest"; \
		mv -f $$cat $$dest && chmod 644 $$dest || exit 1; \
	done

update-mo: $(MO_FILES)

update-po:
	$(MAKE) $(TD).pot
	cd $(localedir); \
	catalogs='$(CATALOGS)'; \
	for cat in $$catalogs; do \
		cat=`basename $$cat`; \
		lang=`echo $$cat | sed 's/\$(CATOBJEXT)$$//'`; \
		mv $$lang.po $$lang.old.po; \
		echo "$$lang:"; \
		if $(MSGMERGE) $$lang.old.po $(TD).pot -o $$lang.po; then \
			rm -f $$lang.old.po; \
		else \
			echo "msgmerge for $$cat failed!"; \
			rm -f $$lang.po; \
			mv $$lang.old.po $$lang.po; \
		fi; \
	done

.SUFFIXES:
.SUFFIXES: .po .mo

.po.mo:
	$(MSGFMT) --verbose -o $@ $<

#	$(MSGFMT) --check --statistics --verbose -o $@ $<


#makedb:
#	./db/initialize.sh


#MSGSRC=$(wildcard locale/*/LC_MESSAGES/messages.po)
#MSGOBJ=$(addprefix $(TARGET_BUILD_PATH)/$(target)/,$(MSG_SRC:.po=.mo))

#gettext: $(MSGOBJS)

#locale/%/LC_MESSAGES/messages.mo: locale/%/LC_MESSAGES/messages.po
#	msgfmt -c $< -o $@


#.PHONY: makedb
