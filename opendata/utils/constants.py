"""
Application-wide constants.

Centralizes magic numbers and string literals for maintainability.
"""

# Security - default secret key (must be overridden in production)
DEFAULT_SECRET_KEY = "your-secret-key-change-this-in-production"

# Data acquisition batch sizes
BATCH_SIZE_LARGE = 5000  # For large datasets (> 10k rows)
BATCH_SIZE_SMALL = 2000  # For smaller datasets
BATCH_SIZE_THRESHOLD = 10000  # Threshold to switch batch sizes

# CSV export
CSV_EXPORT_BATCH_SIZE = 10000
XLSX_EXPORT_ROW_LIMIT = 50000

# User registration
MAX_USERNAME_GENERATION_ATTEMPTS = 1000

# Download progress estimation
ESTIMATED_DOWNLOAD_SECONDS = 30
