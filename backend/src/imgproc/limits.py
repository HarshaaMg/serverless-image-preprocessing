"""Limits shared by both Lambdas.

Kept free of Pillow imports: the job-API Lambda package does not include Pillow.
"""

# INTERVIEW:
# This constant lives in its own tiny module on purpose. It used to be imported
# from image_ops.py, which imports Pillow; the job-API Lambda (packaged WITHOUT
# Pillow) then crashed on AWS with "No module named 'PIL'", even though every
# local test passed. tests/test_packaging.py now guards against that regression.
# Lesson: keep each Lambda's import graph matching what is actually packaged.
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
