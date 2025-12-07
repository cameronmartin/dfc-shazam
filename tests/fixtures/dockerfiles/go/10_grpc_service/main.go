package main

import (
	"log"
	"net"
)

func main() {
	lis, err := net.Listen("tcp", ":50051")
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}
	log.Printf("server listening at %v", lis.Addr())

	// Simple TCP accept loop for testing
	for {
		conn, err := lis.Accept()
		if err != nil {
			log.Printf("failed to accept: %v", err)
			continue
		}
		conn.Close()
	}
}
