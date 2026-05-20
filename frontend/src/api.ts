import type { TrackingResult, ULDResult } from './types'

export async function trackShipment(awb: string): Promise<TrackingResult> {
  const encoded = encodeURIComponent(awb.trim())
  const res = await fetch(`/api/track/${encoded}`)
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
  return data as TrackingResult
}

export async function fetchULD(
  prefix: string,
  awb: string,
  flightNo: string,
  dep: string,
  arr: string,
  depDate: string,
  flrsId: number,
): Promise<ULDResult> {
  const params = new URLSearchParams({
    prefix,
    awb,
    flight_no: flightNo,
    dep,
    arr,
    dep_date: depDate,
    flrs_id: String(flrsId),
  })
  const res = await fetch(`/api/uld?${params}`)
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
  return data as ULDResult
}
