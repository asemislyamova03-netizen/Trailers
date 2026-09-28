# Migration to Flexity

## Target

Trailers Flask is a legacy/reference project.

The target is not to rewrite Trailers as a separate standalone REST API.

The target is to extract domain logic and rebuild it as:

Flexity industry_trailers package.

## Main mapping

| Trailers Flask | Flexity target |
|---|---|
| Customers | parties |
| Leads / requests | workflows / CRM |
| Trailer catalog | catalog + industry_trailers |
| Trailer configurator | industry_trailers.configurator |
| VIN registry | industry_trailers.vin |
| OTTS | industry_trailers.otts |
| Customer orders | sales |
| Contracts | documents |
| Payments | finance |
| Warehouse / stock | inventory |
| Production requests | production + industry_trailers |
| BOM / specifications | production + industry_trailers.bom |
| Shipment | sales + inventory |
| Reports | reporting / finance / data_quality |

## Migration principle

Do not copy code blindly.

For every feature:

1. Describe the business process.
2. Identify current models, routes, templates, and services.
3. Identify whether the logic is universal or trailer-specific.
4. Map universal logic to Flexity modules.
5. Map trailer-specific logic to industry_trailers.
6. Create REST API design.
7. Only then implement in Flexity.

## First migration areas

1. Trailer catalog and configuration.
2. VIN registry.
3. Customer order.
4. Stock availability.
5. Production request.
6. Documents.
7. Shipment.
8. Trailer costing.

## Do not migrate yet

- full tax logic;
- full payroll;
- full AI employees;
- full microservice architecture;
- full accounting replacement;
- full historical data import.

These require separate architecture decisions.