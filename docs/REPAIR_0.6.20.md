# v0.6.20: NBA date-parameter correction

Production probes of v0.6.19 returned 225 football fixtures and 1910 MLB fixtures, and LINE validate/reply accepted the menu, three analysis layouts and available fixture carousels (HTTP 200). NBA CDN returned 403; the ESPN range request returned 400.

This follow-up replaces the rejected range parameter with two independent YYYYMMDD day queries. It removes the redundant daily NBA CDN request. No proxy or access-control evasion is used. 28 local mocked regression tests passed. The requests must still be verified in the deployed environment; a successful deployment alone is not source recovery.
