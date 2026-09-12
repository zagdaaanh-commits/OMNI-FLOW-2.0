
    tailwind.config = {
      darkMode: "class",
      theme: {
        extend: {
          colors: {
            obsidian: {
              DEFAULT: "#0A0B10",
              surface: "rgba(255, 255, 255, 0.03)",
              border: "rgba(255, 255, 255, 0.08)"
            },
            appleIndigo: {
              DEFAULT: "#6366F1",
              glow: "rgba(99, 102, 241, 0.3)"
            },
            appleEmerald: {
              DEFAULT: "#10B981",
              glow: "rgba(16, 185, 129, 0.25)"
            }
          },
          borderRadius: {
            "3xl": "1.5rem",
            "full": "9999px"
          },
          fontFamily: {
            sans: ["Inter", "-apple-system", "BlinkMacSystemFont", "SF Pro Display", "sans-serif"]
          }
        },
      },
    }
  