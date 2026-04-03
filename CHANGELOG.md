# Changelog

## [0.1.1] - 2026-04-04

### Fixed
- Resolve blocking I/O warnings in HA event loop (SSL cert loading, file reads)
- Use persistent HTTP client instead of creating one per request
- Use `asyncio.to_thread` for credential file I/O operations
- Properly close HTTP client on integration unload

### Changed
- Switch to Fermax notification API v2 for push token registration
- Add dedicated user setup guide for doorbell notifications

## [0.1.0] - 2026-04-03

### Added
- Initial release
- Fermax Blue API client with OAuth authentication
- Firebase Cloud Messaging for real-time doorbell notifications
- Door opening via lock entity and button
- Visitor camera (last photo from photocaller)
- Connection status binary sensor
- Doorbell ring binary sensor (auto-resets after 30s)
- WiFi signal strength sensor
- Device status sensor
- Notification enable/disable switch
- Config flow for UI-based setup
- English translations
- HACS compatibility
