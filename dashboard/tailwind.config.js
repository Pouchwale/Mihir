/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // A WhatsApp-business teal-green, deliberately distinct from the emerald used for
        // "success" badges so a primary button never reads as a status.
        // Every shade is defined: an undefined one (e.g. brand-300) silently produces no CSS.
        brand: {
          50: "#effdf9",
          100: "#c9f7ea",
          200: "#93eed6",
          300: "#57dfbe",
          400: "#25c7a3",
          500: "#0fae8b",
          600: "#0a8c71",
          700: "#0c705c",
          800: "#0f594b",
          900: "#11493f",
        },
        canvas: "#f5f7f9",
      },
      boxShadow: {
        card: "0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06)",
      },
    },
  },
  plugins: [],
};
