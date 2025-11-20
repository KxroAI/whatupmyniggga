# Use Python 3.13 slim image
FROM python:3.13-slim

# Install ffmpeg and clean up in one layer to reduce image size
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your code
COPY . .

# Expose port 5000 (used by your Flask keep-alive server)
EXPOSE 5000

# Run the bot
CMD ["python", "main.py"]
