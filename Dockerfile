# Use an official Python runtime as a parent image
FROM python:3.13-slim as base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install uv
RUN pip install --no-cache-dir uv

# Set the working directory in the container
WORKDIR /app

# --- Builder Stage ---
FROM base as builder

# Copy the dependency definitions
COPY pyproject.toml ./
# If you generate a uv.lock file locally, uncomment the next line
# COPY uv.lock ./

# Install dependencies using uv
# Use --frozen-lockfile if you have a uv.lock file
RUN uv pip install --system --no-cache -r pyproject.toml --all-extras
# Alternatively, if using uv.lock:
# RUN uv pip sync --system --no-cache uv.lock

# --- Runtime Stage ---
FROM base as runtime

# Copy the installed dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application code
COPY ./derp ./derp

# Expose port if the application needs it (unlikely for a simple bot)
# EXPOSE 8080

# Define the command to run the application
# This assumes your main script is in derp/main.py
CMD ["python", "-m", "derp.main"] 