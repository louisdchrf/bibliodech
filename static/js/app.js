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
