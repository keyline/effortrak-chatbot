PAGE_SIZE = 50


def report_page(rows, page=1):
    """Bound chat detail, without changing the complete report source or totals."""
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if not 1 <= page <= pages:
        return [], f'This report has {pages} pages. Please choose a page from 1 to {pages}.'
    start = (page - 1) * PAGE_SIZE
    note = ''
    if pages > 1:
        note = (f'Report page: {page} of {pages}. Showing rows {start + 1}-{min(start + PAGE_SIZE, len(rows))} of {len(rows)}. '
                'Report details are paginated. Say "next page" or "page 1" to navigate. '
                'PDF/Excel exports include the complete requested report, not only this page.')
    return rows[start:start + PAGE_SIZE], note
