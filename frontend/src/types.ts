export interface FlightLeg {
  flight_no: string
  from_airport: string
  to_airport: string
  departure_date: string
  departure_time: string
  departure_status: 'actual' | 'estimated' | 'scheduled'
  arrival_date: string
  arrival_time: string
  arrival_status: 'actual' | 'estimated' | 'scheduled'
  flight_time: string
  pieces: number | null
  weight_kg: number | null
  flrs_id: number
}

export interface TrackingResult {
  awb: string
  from_airport: string
  from_name: string
  to_airport: string
  to_name: string
  total_pieces: number | null
  total_weight_kg: number | null
  status: string
  status_code: string
  flights: FlightLeg[]
}

export interface ULDItem {
  uld: string
  pieces: number
}

export interface ULDResult {
  flight_no: string
  departure_date: string
  departure: string
  arrival: string
  ulds: ULDItem[]
}
