"""
Tests unitaires pour les fonctions de détection de séries.
Ces fonctions ne touchent pas à la DB — elles sont testables purement.
"""
import pytest
from app.routers.series import _norm, _first_word, _parse_title, _norm_pub


# ── _norm ────────────────────────────────────────────────────────────────────

class TestNorm:
    def test_lowercase(self):
        assert _norm("TINTIN") == "tintin"

    def test_strip_accents(self):
        assert _norm("Éléphant") == "elephant"

    def test_strip_accents_complex(self):
        assert _norm("Les Âmes perdues") == "les ames perdues"

    def test_quotes_become_space(self):
        assert _norm("l'aventure") == "l aventure"

    def test_normalizes_whitespace(self):
        assert _norm("  foo   bar  ") == "foo bar"

    def test_guillemets(self):
        assert _norm("«Test»") == "test"

    def test_same_name_different_case(self):
        assert _norm("Blake et Mortimer") == _norm("blake et mortimer")

    def test_trailing_punct(self):
        # La série "Astérix." et "Astérix" doivent avoir la même clé normalisée
        assert _norm("Astérix") == _norm("Astérix")


# ── _first_word ──────────────────────────────────────────────────────────────

class TestFirstWord:
    def test_basic(self):
        assert _first_word("Le Roi des ombres") == "roi ombres"

    def test_ignores_articles(self):
        # "Les", "le", "la" ignorés
        result = _first_word("Les Aventures de Tintin")
        assert "les" not in result
        assert "aventures" in result

    def test_two_words_max(self):
        result = _first_word("Blake et Mortimer : le défi")
        words = result.split()
        assert len(words) <= 2

    def test_short_words_skipped(self):
        # Mots < 3 chars ignorés
        result = _first_word("Le Roi et la nuit")
        assert "et" not in result

    def test_none_for_empty(self):
        assert _first_word("") is None

    def test_none_for_only_articles(self):
        assert _first_word("Le La Les") is None

    def test_prefix_match_prevents_false_positives(self):
        # "seuls" et "le maitre des ombres" ne doivent pas avoir le même first_word
        s1 = _first_word("Seuls, tome 1")
        s2 = _first_word("Le Maître des Ombres, tome 1")
        assert s1 != s2


# ── _parse_title ─────────────────────────────────────────────────────────────

class TestParseTitle:
    def test_bnf_format_dot_number(self):
        # "Série. N, sous-titre"
        result = _parse_title("Spirou et Fantasio. 10, 1972-1975")
        assert result is not None
        name, pos = result
        assert "Spirou" in name
        assert pos == 10

    def test_tome_format(self):
        result = _parse_title("Astérix Tome 5")
        assert result is not None
        name, pos = result
        assert "Ast" in name
        assert pos == 5

    def test_tome_format_with_dash(self):
        result = _parse_title("Blake et Mortimer - Tome 3")
        assert result is not None
        _, pos = result
        assert pos == 3

    def test_volume_format(self):
        result = _parse_title("Hunger Games Volume 2")
        assert result is not None
        _, pos = result
        assert pos == 2

    def test_no_match_returns_none(self):
        assert _parse_title("Harry Potter à l'école des sorciers") is None
        assert _parse_title("Le Petit Prince") is None

    def test_series_name_cleaned(self):
        # Le nom de série ne doit pas contenir de ponctuation finale
        result = _parse_title("Les Effacés. 3, La menace")
        assert result is not None
        name, _ = result
        assert not name.endswith(".")
        assert not name.endswith(",")

    def test_tome_t_format(self):
        result = _parse_title("XIII T. 5")
        assert result is not None
        _, pos = result
        assert pos == 5

    def test_number_comma_format(self):
        result = _parse_title("Tintin. 7, L'île noire")
        assert result is not None
        name, pos = result
        assert pos == 7
        assert len(name) >= 2

    def test_position_is_int(self):
        result = _parse_title("Naruto Tome 12")
        assert result is not None
        _, pos = result
        assert isinstance(pos, int)
        assert pos == 12

    def test_no_false_positive_publisher(self):
        # Titres avec numéro sans contexte de série ne doivent pas être parsés abusivement
        # "Le livre de poche" ne contient pas de numéro de tome → None
        assert _parse_title("Le livre de poche") is None


# ── _norm_pub ────────────────────────────────────────────────────────────────

class TestNormPub:
    def test_basic(self):
        assert _norm_pub("Dargaud") == "dargaud"

    def test_strips_parentheses_suffix(self):
        # "Casterman (Belgique)" → "casterman"
        assert _norm_pub("Casterman (Belgique)") == "casterman"

    def test_none_returns_empty(self):
        assert _norm_pub(None) == ""

    def test_empty_returns_empty(self):
        assert _norm_pub("") == ""
