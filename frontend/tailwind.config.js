/** Build: npx tailwindcss@3 -c frontend/tailwind.config.js -i frontend/input.css -o static/vendor/app.css --minify */
module.exports = {
  content: ['./static/*.html', './static/*.js'],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'Segoe UI', 'system-ui', '-apple-system', 'Roboto', 'sans-serif'] },
      colors: { brand: { 50: '#ecfdf5', 100: '#d1fae5', 500: '#10b981', 600: '#059669', 700: '#047857', 900: '#064e3b' } },
    },
  },
};
