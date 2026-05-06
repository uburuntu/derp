FROM oven/bun:1 AS base
WORKDIR /app

# Install system dependencies (ffmpeg for media conversion)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Install dependencies, including drizzle-kit for deploy-time migrations
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile

# Copy source
COPY . .

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

CMD ["bun", "run", "src/index.ts"]
