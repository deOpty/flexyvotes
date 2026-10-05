from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from core.tests.factories import institutional_setup, open_institutional


class FrenchLocaleTests(TestCase):
    """Voter-facing pages render in French when the visitor picks French."""

    def setUp(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = 'fr'

    def test_catalog_is_compiled(self):
        with translation.override('fr'):
            self.assertEqual(translation.gettext('Sign in to vote'), 'Se connecter pour voter')
            self.assertEqual(translation.gettext('Try again in about %(retry_after)s seconds.') % {'retry_after': 5},
                             'Réessayez dans environ 5 secondes.')

    def test_public_pages_in_french(self):
        response = self.client.get(reverse('accessibility'))
        self.assertContains(response, 'lang="fr"')
        self.assertContains(response, 'Aller au contenu')
        self.assertContains(response, 'Accessibilité et faible débit')
        self.assertContains(response, 'Enregistrer les préférences')

    def test_ballot_flow_in_french(self):
        s = institutional_setup()
        event = open_institutional(s['event'], s['admin'], s['reviewer'])
        response = self.client.get(reverse('elections:vote_start', args=[event.pk]))
        self.assertContains(response, 'Se connecter pour voter')
        code = s['codes'][s['voters'][0].pk]
        self.client.post(reverse('elections:vote_start', args=[event.pk]), {'action': 'code', 'code': code})
        response = self.client.get(reverse('elections:vote_ballot', args=[event.pk]))
        self.assertContains(response, 'Vérifier mes choix')

    def test_english_remains_default(self):
        self.client.cookies.pop(settings.LANGUAGE_COOKIE_NAME)
        response = self.client.get(reverse('accessibility'))
        self.assertContains(response, 'Skip to content')
