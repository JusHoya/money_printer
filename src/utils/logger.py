import logging
import os
from datetime import datetime

def setup_logger(name="MoneyPrinter", level=logging.INFO):
    """
    Configures a shared logger that writes to a unique timestamped file.
    """
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"money_printer_{timestamp}.log"
    
    # Create a custom logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False # Prevent double logging
    
    # Check if handlers already exist (critical for shared process space)
    if logger.handlers:
        return logger

    # Create handlers
    file_path = os.path.join(log_dir, log_file)
    f_handler = logging.FileHandler(file_path, encoding='utf-8')
    f_handler.setLevel(level)

    # Create formatters and add it to handlers
    log_format = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    f_handler.setFormatter(log_format)

    # Add handlers to the logger
    logger.addHandler(f_handler)
    
    return logger

# Singleton Accessor
logger = setup_logger()
