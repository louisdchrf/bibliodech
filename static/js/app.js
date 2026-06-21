// Bibliodech – shared JS utilities
// Most logic lives inline in each template's {% block scripts %}.
// This file provides shared helpers loaded on every page.

(function () {
  // Automatically dismiss flash messages after 4 seconds
  const flash = document.querySelector('.flash');
  if (flash) {
    setTimeout(() => {
      flash.style.transition = 'opacity .4s ease';
      flash.style.opacity = '0';
      setTimeout(() => flash.remove(), 400);
    }, 4000);
  }
})();

// Timezone configuré (chargé depuis l'API au démarrage)
window.APP_TIMEZONE = 'Europe/Paris';
fetch('/api/settings')
  .then(r => r.ok ? r.json() : null)
  .then(d => { if (d && d.timezone) window.APP_TIMEZONE = d.timezone; })
  .catch(() => {});

/**
 * Formate une date ISO UTC en date/heure lisible selon le fuseau configuré.
 */
window.fmtDateTime = function(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('fr-FR', { timeZone: window.APP_TIMEZONE });
};

window.fmtDate = function(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('fr-FR', { timeZone: window.APP_TIMEZONE });
};
