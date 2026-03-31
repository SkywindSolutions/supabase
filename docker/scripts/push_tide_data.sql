-- Sample source query for push_tide_data.py.
-- Replace `your_source_table` and any filter columns to match the hourly
-- forecast table on the remote server.

SELECT
    dtg_gmt AS forecastdtutc,
    dtg_gmt AS "timestamp",
    datum,
    astronomical_forecast_fd,
    tidal_observations_ft,
    tidemean,
    tidelb,
    tideub
FROM your_source_table
WHERE station_id = 8726724
  AND dtg_gmt >= NOW() - INTERVAL '2 hours'
ORDER BY dtg_gmt ASC;