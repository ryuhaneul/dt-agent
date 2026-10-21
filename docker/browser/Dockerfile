# Browser control server — headless Chromium via Playwright + Express API.
# Runs inside ductor-net, accessible only by ductor-bot.

FROM mcr.microsoft.com/playwright:v1.59.1-noble

WORKDIR /app
COPY control-server/ .
RUN npm ci --omit=dev \
    && chown -R pwuser:pwuser /app

# Run as non-root (pwuser is the built-in Playwright user)
USER pwuser

EXPOSE 18800

CMD ["node", "server.js"]
