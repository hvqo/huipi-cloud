"""Only exact authenticated OMR paths are accepted."""

POLICIES = {
    "omr_paper_view": ("GET", "/omr/papers/{paper_id}", "paper_read"),
    "omr_create_template": ("POST", "/omr/papers/{paper_id}", "paper_write"),
    "omr_print_template": ("GET", "/omr/templates/{template_id}/print.pdf", "template"),
    "omr_current": ("GET", "/answer-scripts/{script_id}/omr", "script_read"),
    "omr_recognize": ("POST", "/answer-scripts/{script_id}/omr", "script_write"),
    "omr_inspect": ("GET", "/omr/recognitions/{recognition_id}", "recognition_read"),
    "omr_image": ("GET", "/omr/recognitions/{recognition_id}/image", "recognition_read"),
    "omr_confirm": ("POST", "/omr/recognitions/{recognition_id}/confirm", "recognition_write"),
}
