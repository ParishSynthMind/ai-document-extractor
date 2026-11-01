| Document | Read via | Fields correct | Sent to review | Wrong and caught by review | Wrong and not caught |
|---|---|---|---|---|---|
| brightwater_invoice.pdf | pdf-text | 11/11 | 0 | 0 | 0 |
| kestrel_invoice_scanned.pdf | ocr | 9/11 | 4 | 2 | 0 |
| oakfield_invoice.xlsx | xlsx | 11/11 | 0 | 0 | 0 |
| permit_application_riverside.docx | docx | 11/11 | 1 | 0 | 0 |
| staff_credential_export.csv | csv | 9/9 | 0 | 0 | 0 |
| **All** | | **51/53 (96%)** | 5 | 2 | 0 |

- BAD kestrel_invoice_scanned.pdf invoice_date: expected '2026-10-03', got '2026-03-10' (low, flagged)
- OK  kestrel_invoice_scanned.pdf due_date: expected '2026-11-02', got '2026-11-02' (low, flagged)
- BAD kestrel_invoice_scanned.pdf purchase_order: expected 'PO-77198', got None (low, flagged)
- OK  kestrel_invoice_scanned.pdf bank_iban: expected 'AE070331234567890123456', got 'AE070331234567890123456' (low, flagged)
- OK  permit_application_riverside.docx contractor_licence_number: expected None, got None (high, flagged)
